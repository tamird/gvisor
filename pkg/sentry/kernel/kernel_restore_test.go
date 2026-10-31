// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package kernel

import (
	"bytes"
	"errors"
	"io"
	"os"
	"runtime"
	"testing"
	"testing/synctest"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/hostarch"
	"gvisor.dev/gvisor/pkg/sentry/checkpoint"
	"gvisor.dev/gvisor/pkg/sentry/contexttest"
	"gvisor.dev/gvisor/pkg/sentry/pgalloc"
	"gvisor.dev/gvisor/pkg/sentry/state/stateio"
	"gvisor.dev/gvisor/pkg/sentry/state/stateipc"
	"gvisor.dev/gvisor/pkg/unet"
	"gvisor.dev/gvisor/pkg/urpc"
)

func TestAsyncMFLoaderStartupError(t *testing.T) {
	// The bubble detects a missing mainMFStartWg completion as a deadlock,
	// without a timeout. This test does not depend on virtual time.
	synctest.Test(t, func(t *testing.T) {
		// A sub-page read limit fails before metadata or mainMF is loaded.
		pages := stateio.NewIOReader(bytes.NewReader(nil), 1, 1, 1)
		metadata := io.NopCloser(bytes.NewReader(nil))
		loader := NewAsyncMFLoader(metadata, pages, nil, nil, pgalloc.AsyncPagesFileLoadOpts{Throttled: true})
		defer loader.Unthrottle()

		startErr := loader.WaitMainMFStart()
		if startErr == nil {
			t.Fatal("WaitMainMFStart() succeeded with a sub-page read limit")
		}
		if err := loader.WaitMetadata(); err != startErr {
			t.Errorf("WaitMetadata() = %v, want %v", err, startErr)
		}
		if err := loader.Wait(); err != startErr {
			t.Errorf("Wait() = %v, want %v", err, startErr)
		}
	})
}

func TestCheckpointRegistrationDuringNotification(t *testing.T) {
	var k Kernel
	w := &k.CheckpointWait
	w.k = &k
	gen := CheckpointGeneration{Count: 1}
	wantErr := errors.New("checkpoint failed")
	type result struct {
		gen CheckpointGeneration
		err error
	}
	results := make(chan result, 2)
	registered := make(chan any, 1)
	notified := make(chan struct{})

	// Hold the generation lock as a checkpoint producer does. Registration
	// must not hold the waiters lock while waiting to read this generation.
	k.checkpointMu.Lock()
	k.checkpointGen = gen
	go func() {
		registered <- w.Register(func(gen CheckpointGeneration, err error) {
			results <- result{gen, err}
		}, gen.Count)
	}()
	go func() {
		for {
			w.mu.Lock()
			pending := len(w.waiters) != 0
			w.mu.Unlock()
			if pending {
				break
			}
			runtime.Gosched()
		}
		w.signal(gen, wantErr)
		close(notified)
	}()

	// Mutex contention is not durably blocking in synctest, so its clock
	// cannot advance a watchdog when the original lock inversion occurs.
	// Use a real-time deadline to release both goroutines on that failure.
	var blocked bool
	select {
	case <-notified:
	case <-time.After(5 * time.Second):
		blocked = true
	}
	// Release the generation lock even on failure, so the original lock
	// inversion cannot strand either goroutine during test cleanup.
	k.checkpointMu.Unlock()
	key := <-registered
	<-notified
	w.Unregister(key)
	if blocked {
		t.Fatal("checkpoint notification blocked on registration")
	}
	if len(results) != 1 {
		t.Fatalf("got %d callbacks, want 1", len(results))
	}
	if got := <-results; got.gen != gen || got.err != wantErr {
		t.Fatalf("callback = (%+v, %v), want (%+v, %v)", got.gen, got.err, gen, wantErr)
	}
}

// WaitForCheckpoint is a next-generation API, not a completion query for a
// checkpoint that the caller may have already triggered.
func TestWaitForNextCheckpoint(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		var k Kernel
		k.CheckpointWait.k = &k
		k.OnCheckpointAttempt(nil)

		result := make(chan error, 1)
		go func() {
			result <- k.WaitForCheckpoint()
		}()
		synctest.Wait()
		select {
		case err := <-result:
			t.Fatalf("WaitForCheckpoint returned for an earlier generation: %v", err)
		default:
		}

		wantErr := errors.New("next checkpoint failed")
		k.OnCheckpointAttempt(wantErr)
		if got, want := <-result, wantErr; got != want {
			t.Errorf("WaitForCheckpoint() = %v, want %v", got, want)
		}
	})
}

// fullPageServer serves /dev/full through the real checkpoint gofer protocol.
type fullPageServer struct{}

func (fullPageServer) Destroy()                                     {}
func (fullPageServer) OpenRead(string) (stateio.AsyncReader, error) { return nil, os.ErrPermission }
func (fullPageServer) OpenWrite(string) (stateio.AsyncWriter, error) {
	f, err := os.OpenFile("/dev/full", os.O_WRONLY, 0)
	if err != nil {
		return nil, err
	}
	return stateio.NewIOWriter(f, uint64(hostarch.PageSize), 1, 1), nil
}

type observedPageClose struct {
	stateio.AsyncWriter
	done chan struct{}
}

func (w observedPageClose) Close() error {
	err := w.AsyncWriter.Close()
	close(w.done)
	return err
}

// Hold the first file's metadata until its page write has failed and the
// client has closed its handle. The next file is then saved by the real
// kernel checkpoint loop under the same ordering as a slow metadata sink.
type metadataAfterPageClose chan struct{}

func (done metadataAfterPageClose) Write(p []byte) (int, error) { <-done; return len(p), nil }
func (metadataAfterPageClose) Close() error                     { return nil }

func TestKernelCheckpointReportsFullDevice(t *testing.T) {
	server, err := stateipc.NewAsyncFileServer(fullPageServer{})
	if err != nil {
		t.Fatal(err)
	}
	u := urpc.NewServer()
	u.Register(server)
	cl, srv, err := unet.SocketPair(false)
	if err != nil {
		t.Fatal(err)
	}
	u.StartHandling(srv)
	t.Cleanup(func() { u.Stop(0) })
	client, err := stateipc.NewAsyncFileClient(urpc.NewClient(cl))
	if err != nil {
		t.Fatal(err)
	}
	defer client.DecRef()
	writer, err := client.OpenWrite("pages")
	if err != nil {
		t.Fatal(err)
	}
	ctx := contexttest.Context(t)
	main := pgalloc.MemoryFileFromContext(ctx)
	priv := pgalloc.MemoryFileFromContext(contexttest.Context(t))
	for _, f := range []*pgalloc.MemoryFile{main, priv} {
		t.Cleanup(f.Destroy)
		fr, err := f.Allocate(2*uint64(hostarch.PageSize), pgalloc.AllocOpts{Mode: pgalloc.AllocateUncommitted})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { f.DecRef(fr) })
		for page := range 2 {
			if _, err := unix.Pwrite(f.FD(), []byte{1}, int64(fr.Start)+int64(page)*int64(hostarch.PageSize)); err != nil {
				t.Fatal(err)
			}
		}
	}
	done := make(chan struct{})
	k := Kernel{mf: main}
	got := k.saveMemoryFiles(ctx, nil, metadataAfterPageClose(done), observedPageClose{writer, done}, map[checkpoint.ResourceID]*pgalloc.MemoryFile{{Path: "/private"}: priv}, false, true)
	if want := unix.ENOSPC; !errors.Is(got, want) {
		t.Fatalf("kernel checkpoint: got %v, want %v", got, want)
	}
	t.Logf("kernel checkpoint preserved errno: %v", got)
}
