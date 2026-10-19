// Copyright 2018 The gVisor Authors.
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

package pipe

import (
	"bytes"
	"io"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/errors/linuxerr"
	"gvisor.dev/gvisor/pkg/sentry/contexttest"
	"gvisor.dev/gvisor/pkg/sentry/vfs"
	"gvisor.dev/gvisor/pkg/usermem"
	"gvisor.dev/gvisor/pkg/waiter"
)

func runTest(t *testing.T, sizeBytes int64, tester func(ctx context.Context, r, w *vfs.FileDescription)) {
	ctx := contexttest.Context(t)
	vfsObj := &vfs.VirtualFilesystem{}
	if err := vfsObj.Init(ctx); err != nil {
		t.Fatalf("VFS init: %v", err)
	}
	vd := vfsObj.NewAnonVirtualDentry("pipe")
	defer vd.DecRef(ctx)

	vp := NewVFSPipe(false /* isNamed */, sizeBytes)
	r, w, err := vp.ReaderWriterPair(ctx, vd.Mount(), vd.Dentry(), 0)
	if err != nil {
		t.Fatalf("ReaderWriterPair failed: %v", err)
	}
	defer r.DecRef(ctx)
	defer w.DecRef(ctx)

	tester(ctx, r, w)
}

func TestReadDiagnosticDoesNotConsumePipeOrNotification(t *testing.T) {
	runTest(t, 65536, func(ctx context.Context, r, w *vfs.FileDescription) {
		e, ch := waiter.NewChannelEntry(waiter.ReadableEvents | waiter.EventHUp)
		r.EventRegister(&e)
		defer r.EventUnregister(&e)
		p := r.Impl().(*VFSPipeFD)
		d := NewReadDiagnostics(time.Hour, "copier")
		id := ReadIdentity{TID: 11, TGID: 10, FD: 26, Name: "copier_1"}
		buf := make([]byte, 1)
		if _, err := r.Read(ctx, usermem.BytesIOSequence(buf), vfs.ReadOptions{}); err != linuxerr.ErrWouldBlock {
			t.Fatalf("empty pipe with writer: %v", err)
		}
		observation := p.ObserveRead(d, id, ch)
		defer observation.End()
		if _, err := w.Write(ctx, usermem.BytesIOSequence([]byte("x")), vfs.WriteOptions{}); err != nil {
			t.Fatal(err)
		}
		report, ok := observation.observer.sample(observation.due)
		if !ok || !report.Unchanged || report.Identity != id || !report.State.LockAcquired || report.State.Size != 1 || report.PendingBefore != 1 || report.PendingAfter != 1 {
			t.Fatalf("unexpected sample: %+v, emitted=%t", report, ok)
		}
		select {
		case <-ch:
		default:
			t.Fatal("sample consumed the real waiter notification")
		}
		if n, err := r.Read(ctx, usermem.BytesIOSequence(buf), vfs.ReadOptions{}); n != 1 || err != nil || buf[0] != 'x' {
			t.Fatalf("sample changed pipe data: n=%d err=%v buf=%q", n, err, buf)
		}
	})
}

func TestReadDiagnosticWaitIdentityAndReuse(t *testing.T) {
	runTest(t, 65536, func(ctx context.Context, r, _ *vfs.FileDescription) {
		p := r.Impl().(*VFSPipeFD)
		d := NewReadDiagnostics(time.Hour, "copier")
		id := ReadIdentity{TID: 11, FD: 26, Name: "copier"}
		_, ch := waiter.NewChannelEntry(waiter.ReadableEvents)
		if got := p.ObserveRead(nil, id, ch); got != nil {
			t.Fatal("disabled observer created a wait")
		}
		if got := p.ObserveRead(d, ReadIdentity{Name: "other"}, ch); got != nil || p.readObserver != nil {
			t.Fatal("unselected task allocated an observer")
		}
		first := p.ObserveRead(d, id, ch)
		timer := first.observer.timer
		if got := p.ObserveRead(d, ReadIdentity{TID: 12, Name: "copier"}, ch); got != nil || d.skipped.Load() != 1 {
			t.Fatal("concurrent reader overwrote the selected wait")
		}
		first.End()
		if _, ok := first.observer.sample(first.due); ok {
			t.Fatal("completed wait was reported as active")
		}
		second := p.ObserveRead(d, id, ch)
		defer second.End()
		if second.observer.timer != timer || second.generation <= first.generation || d.created.Load() != 1 {
			t.Fatal("new wait did not reuse timer with a new identity")
		}
		if _, ok := second.observer.sample(second.due.Add(-time.Nanosecond)); ok {
			t.Fatal("old callback sampled a new wait before its due time")
		}
		if report, ok := second.observer.sample(second.due); !ok || report.Generation != second.generation {
			t.Fatalf("new generation was not observed: %+v %t", report, ok)
		}
		if _, ok := second.observer.sample(second.due); ok {
			t.Fatal("generation was reported twice")
		}
	})
}

func TestReadDiagnosticBusyPipe(t *testing.T) {
	runTest(t, 65536, func(ctx context.Context, r, _ *vfs.FileDescription) {
		p := r.Impl().(*VFSPipeFD)
		locked, release, done := make(chan struct{}), make(chan struct{}), make(chan struct{})
		go func() {
			p.pipe.mu.Lock()
			close(locked)
			<-release
			p.pipe.mu.Unlock()
			close(done)
		}()
		defer func() { close(release); <-done }()
		<-locked
		if got := p.pipe.sampleRead(); got.LockAcquired {
			t.Fatalf("busy pipe falsely reported a buffer snapshot: %+v", got)
		}
	})
}

func TestReadDiagnosticWriterLifetime(t *testing.T) {
	ctx := contexttest.Context(t)
	var vfsObj vfs.VirtualFilesystem
	if err := vfsObj.Init(ctx); err != nil {
		t.Fatal(err)
	}
	vd := vfsObj.NewAnonVirtualDentry("pipe")
	defer vd.DecRef(ctx)
	vp := NewVFSPipe(false, DefaultPipeSize)
	r, w, err := vp.ReaderWriterPair(ctx, vd.Mount(), vd.Dentry(), 0)
	if err != nil {
		t.Fatal(err)
	}
	defer r.DecRef(ctx)
	d := NewReadDiagnostics(time.Hour, "copier")
	reader := r.Impl().(*VFSPipeFD)
	writer := w.Impl().(*VFSPipeFD)
	reader.EnrollWriter(d, writer, ReadIdentity{TID: 10, Name: "creator"})
	// A writable reopen creates another description, not another reference
	// to the original description. Its released record must remain visible.
	reopened, err := vp.Open(ctx, vd.Mount(), vd.Dentry(), linux.O_WRONLY, &vfs.FileLocks{})
	if err != nil {
		w.DecRef(ctx)
		t.Fatal(err)
	}
	reopened.DecRef(ctx)
	initial := vp.Pipe().sampleWriters(time.Now())
	if !initial.Enrolled || initial.Incomplete != 0 || len(initial.Writers) != 2 || initial.Writers[0].ID == initial.Writers[1].ID || initial.Writers[0].RefsAfter != 1 || initial.Writers[1].RefsAfter != 0 || initial.Writers[1].PhaseAfter != releaseComplete {
		w.DecRef(ctx)
		t.Fatalf("initial/reopened description identity: %+v", initial)
	}
	// Retain a real extra reference, like a reference outside an FD table.
	w.IncRef()
	w.DecRef(ctx)
	defer func() {
		if w != nil {
			w.DecRef(ctx)
		}
	}()
	e, ch := waiter.NewChannelEntry(waiter.ReadableEvents | waiter.EventHUp)
	r.EventRegister(&e)
	defer r.EventUnregister(&e)
	var duringRelease writerSnapshot
	observeRelease := waiter.NewFunctionEntry(waiter.EventHUp, func(waiter.EventMask) {
		duringRelease = vp.Pipe().sampleWriters(time.Now())
	})
	r.EventRegister(&observeRelease)
	defer r.EventUnregister(&observeRelease)
	if got := vp.Pipe().sampleRead(); got.WritersBefore != 1 || got.WritersAfter != 1 {
		t.Fatalf("extra writer reference not retained: %+v", got)
	}
	buf := usermem.BytesIOSequence(make([]byte, 1))
	if _, err := r.Read(ctx, buf, vfs.ReadOptions{}); err != linuxerr.ErrWouldBlock {
		t.Fatalf("live extra writer should prevent EOF: %v", err)
	}
	operation := writer.ObserveWrite(ReadIdentity{TID: 11, FD: 2, Name: "writer"}, "write")
	operation.DecRefStarted()
	w.DecRef(ctx)
	w = nil
	operation.Done()
	if len(duringRelease.Writers) != 2 || duringRelease.Writers[0].RefsAfter != 0 || duringRelease.Writers[0].PhaseAfter != releaseWriterClosed || len(duringRelease.Writes) != 1 || duringRelease.Writes[0].PhaseAfter != writeDecRefStarted {
		t.Fatalf("actual notification did not expose final release progress: %+v", duringRelease)
	}
	finished := vp.Pipe().sampleWriters(time.Now())
	const allReleaseMilestones = 1<<releaseFinalCleanup | 1<<releasePipeEntered | 1<<releaseWriterClosed | 1<<releaseNotified | 1<<releaseComplete
	if finished.Incomplete != 0 || len(finished.Writers) != 2 || finished.Writers[0].PhaseAfter != releaseComplete || finished.Writers[0].Milestones != allReleaseMilestones || len(finished.Writes) != 0 {
		t.Fatalf("released tombstone or completed operation lost: %+v", finished)
	}
	if got := vp.Pipe().sampleRead(); !got.LockAcquired || got.WritersBefore != 0 || got.WritersAfter != 0 || got.Size != 0 {
		t.Fatalf("final writer release not observed: %+v", got)
	}
	select {
	case <-ch:
	default:
		t.Fatal("final writer release did not notify registered reader")
	}
	if n, err := r.Read(ctx, buf, vfs.ReadOptions{}); n != 0 || err != io.EOF {
		t.Fatalf("final writer release should yield EOF: %d %v", n, err)
	}
	// A reader registering after close also observes HUP synchronously.
	e2, ch2 := waiter.NewChannelEntry(waiter.EventHUp)
	r.EventRegister(&e2)
	defer r.EventUnregister(&e2)
	select {
	case <-ch2:
	default:
		t.Fatal("registration after close lost HUP")
	}
}

func TestReadDiagnosticTimerBudgetReleasedWithFile(t *testing.T) {
	runTest(t, 65536, func(ctx context.Context, r, _ *vfs.FileDescription) {
		d := NewReadDiagnostics(time.Hour, "copier")
		id := ReadIdentity{Name: "copier"}
		_, ch := waiter.NewChannelEntry(waiter.ReadableEvents)
		var files []*vfs.FileDescription
		defer func() {
			for _, f := range files {
				if f != nil {
					f.DecRef(ctx)
				}
			}
		}()
		var last *VFSPipeFD
		var first *ReadObservation
		for i := 0; i <= maxReadObservers; i++ {
			vp := NewVFSPipe(false, DefaultPipeSize)
			reader, writer, err := vp.ReaderWriterPair(ctx, r.Mount(), r.Dentry(), 0)
			if err != nil {
				t.Fatal(err)
			}
			files = append(files, reader, writer)
			last = reader.Impl().(*VFSPipeFD)
			observation := last.ObserveRead(d, id, ch)
			if i == 0 {
				first = observation
			}
			if i < maxReadObservers && observation == nil {
				t.Fatalf("observer %d rejected within budget", i)
			}
			if i == maxReadObservers && observation != nil {
				t.Fatal("live timer budget exceeded")
			}
			if i != 0 {
				observation.End()
			}
		}
		if d.live.Load() != maxReadObservers || d.skipped.Load() != 1 {
			t.Fatalf("unexpected budget state: live=%d skipped=%d", d.live.Load(), d.skipped.Load())
		}
		// Idle cached timers also consume the budget; releasing their real file
		// descriptions cancels the timer and makes that capacity available.
		files[0].DecRef(ctx)
		files[0] = nil
		if _, ok := first.observer.sample(first.due); ok {
			t.Fatal("released file retained an active observation")
		}
		first.End() // Closing the file also invalidates a later End.
		observation := last.ObserveRead(d, id, ch)
		if observation == nil {
			t.Fatal("file release did not return timer capacity")
		}
		observation.End()
	})
}

func TestPipeRW(t *testing.T) {
	runTest(t, 65536, func(ctx context.Context, r *vfs.FileDescription, w *vfs.FileDescription) {
		msg := []byte("here's some bytes")
		wantN := int64(len(msg))
		n, err := w.Write(ctx, usermem.BytesIOSequence(msg), vfs.WriteOptions{})
		if n != wantN || err != nil {
			t.Fatalf("Writev: got (%d, %v), wanted (%d, nil)", n, err, wantN)
		}

		buf := make([]byte, len(msg))
		n, err = r.Read(ctx, usermem.BytesIOSequence(buf), vfs.ReadOptions{})
		if n != wantN || err != nil || !bytes.Equal(buf, msg) {
			t.Fatalf("Readv: got (%d, %v) %q, wanted (%d, nil) %q", n, err, buf, wantN, msg)
		}
	})
}

func TestPipeReadBlock(t *testing.T) {
	runTest(t, 65536, func(ctx context.Context, r *vfs.FileDescription, w *vfs.FileDescription) {
		n, err := r.Read(ctx, usermem.BytesIOSequence(make([]byte, 1)), vfs.ReadOptions{})
		if n != 0 || err != linuxerr.ErrWouldBlock {
			t.Fatalf("Readv: got (%d, %v), wanted (0, %v)", n, err, linuxerr.ErrWouldBlock)
		}
	})
}

func TestPipeWriteBlock(t *testing.T) {
	const atomicIOBytes = 2
	const capacity = MinimumPipeSize

	runTest(t, capacity, func(ctx context.Context, r *vfs.FileDescription, w *vfs.FileDescription) {
		msg := make([]byte, capacity+1)
		n, err := w.Write(ctx, usermem.BytesIOSequence(msg), vfs.WriteOptions{})
		if wantN, wantErr := int64(capacity), linuxerr.ErrWouldBlock; n != wantN || err != wantErr {
			t.Fatalf("Writev: got (%d, %v), wanted (%d, %v)", n, err, wantN, wantErr)
		}
	})
}

func TestPipeWriteUntilEnd(t *testing.T) {
	const atomicIOBytes = 2
	runTest(t, atomicIOBytes, func(ctx context.Context, r *vfs.FileDescription, w *vfs.FileDescription) {
		msg := []byte("here's some bytes")

		wDone := make(chan struct{})
		rDone := make(chan struct{})
		defer func() {
			// Signal the reader to stop and wait until it does so.
			close(wDone)
			<-rDone
		}()

		go func() {
			defer close(rDone)
			// Read from r until done is closed.
			ctx := contexttest.Context(t)
			buf := make([]byte, len(msg)+1)
			dst := usermem.BytesIOSequence(buf)
			e, ch := waiter.NewChannelEntry(waiter.ReadableEvents)
			r.EventRegister(&e)
			defer r.EventUnregister(&e)
			for {
				n, err := r.Read(ctx, dst, vfs.ReadOptions{})
				dst = dst.DropFirst64(n)
				if err == linuxerr.ErrWouldBlock {
					select {
					case <-ch:
						continue
					case <-wDone:
						// We expect to have 1 byte left in dst since len(buf) ==
						// len(msg)+1.
						if dst.NumBytes() != 1 || !bytes.Equal(buf[:len(msg)], msg) {
							t.Errorf("Reader: got %q (%d bytes remaining), wanted %q", buf, dst.NumBytes(), msg)
						}
						return
					}
				}
				if err != nil {
					t.Errorf("Readv: got unexpected error %v", err)
					return
				}
			}
		}()

		src := usermem.BytesIOSequence(msg)
		e, ch := waiter.NewChannelEntry(waiter.WritableEvents)
		w.EventRegister(&e)
		defer w.EventUnregister(&e)
		for src.NumBytes() != 0 {
			n, err := w.Write(ctx, src, vfs.WriteOptions{})
			src = src.DropFirst64(n)
			if err == linuxerr.ErrWouldBlock {
				<-ch
				continue
			}
			if err != nil {
				t.Fatalf("Writev: got (%d, %v)", n, err)
			}
		}
	})
}
