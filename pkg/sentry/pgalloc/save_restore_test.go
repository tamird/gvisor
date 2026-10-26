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

package pgalloc

import (
	"bytes"
	"context"
	"encoding/binary"
	"io"
	"os"
	"runtime"
	"sync"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/hostarch"
	"gvisor.dev/gvisor/pkg/memutil"
	"gvisor.dev/gvisor/pkg/sentry/memmap"
	"gvisor.dev/gvisor/pkg/sentry/usage"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/wire"
)

func newSaveTestMemoryFile(t *testing.T, diskBacked bool) *MemoryFile {
	t.Helper()
	var file *os.File
	if diskBacked {
		var err error
		if file, err = os.CreateTemp(t.TempDir(), "pgalloc-save-test"); err != nil {
			t.Fatalf("CreateTemp: %v", err)
		}
	} else {
		fd, err := memutil.CreateMemFD("pgalloc-save-test", 0)
		if err != nil {
			t.Fatalf("CreateMemFD: %v", err)
		}
		file = os.NewFile(uintptr(fd), "pgalloc-save-test")
	}
	f, err := NewMemoryFile(file, MemoryFileOpts{
		DelayedEviction:         DelayedEvictionDisabled,
		DisableMemoryAccounting: true,
		DiskBackedFile:          diskBacked,
	})
	if err != nil {
		file.Close()
		t.Fatalf("NewMemoryFile: %v", err)
	}
	t.Cleanup(f.Destroy)
	return f
}

func allocateSaveTestPages(t *testing.T, f *MemoryFile) memmap.FileRange {
	t.Helper()
	fr, err := f.Allocate(16*uint64(hostarch.PageSize), AllocOpts{Mode: AllocateUncommitted})
	if err != nil {
		t.Fatalf("Allocate: %v", err)
	}
	t.Cleanup(func() { f.DecRef(fr) })
	return fr
}

func pwritePage(t *testing.T, f *MemoryFile, fr memmap.FileRange, page int, b byte) {
	t.Helper()
	buf := make([]byte, hostarch.PageSize)
	buf[17] = b
	if _, err := unix.Pwrite(f.FD(), buf, int64(fr.Start)+int64(page*int(hostarch.PageSize))); err != nil {
		t.Fatalf("Pwrite page %d: %v", page, err)
	}
}

func TestHostFileDataSeeker(t *testing.T) {
	f := newSaveTestMemoryFile(t, false)
	fr := allocateSaveTestPages(t, f)
	pwritePage(t, f, fr, 2, 1)
	pwritePage(t, f, fr, 3, 1)
	pwritePage(t, f, fr, 9, 1)
	pwritePage(t, f, fr, 12, 0)
	page := func(n uint64) uint64 { return fr.Start + n*uint64(hostarch.PageSize) }
	d := f.newHostFileDataSeeker()
	for _, call := range []struct {
		off  uint64
		want memmap.FileRange
	}{
		{off: page(0), want: memmap.FileRange{page(2), page(4)}},
		{off: page(3), want: memmap.FileRange{page(3), page(4)}},
		{off: page(4), want: memmap.FileRange{page(9), page(10)}},
		{off: page(10), want: memmap.FileRange{page(12), page(13)}},
		{off: page(13), want: memmap.FileRange{f.TotalSize(), f.TotalSize()}},
		{off: page(15), want: memmap.FileRange{f.TotalSize(), f.TotalSize()}},
	} {
		got, err := d.dataAtOrAfter(call.off)
		if err != nil || got != call.want {
			t.Errorf("dataAtOrAfter(%#x): got (%v, %v), want (%v, nil)", call.off, got, err, call.want)
		}
	}
}

func TestSaveToPreservesContents(t *testing.T) {
	for _, tc := range []struct {
		name          string
		diskBacked    bool
		exclude       bool
		wantCommitted uint64
	}{
		{name: "memfd", wantCommitted: 2 * uint64(hostarch.PageSize)},
		{name: "memfd excluding committed zero pages", exclude: true, wantCommitted: uint64(hostarch.PageSize)},
		{name: "disk-backed", diskBacked: true, wantCommitted: 2 * uint64(hostarch.PageSize)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newSaveTestMemoryFile(t, tc.diskBacked)
			fr := allocateSaveTestPages(t, f)
			pwritePage(t, f, fr, 2, 1)
			pwritePage(t, f, fr, 13, 2)
			if err := f.SaveTo(context.Background(), io.Discard, &SaveOpts{}); err != nil {
				t.Fatalf("first SaveTo: %v", err)
			}
			pwritePage(t, f, fr, 9, 0)
			pwritePage(t, f, fr, 13, 0)
			want := make([]byte, fr.Length())
			want[2*hostarch.PageSize+17] = 1

			var checkpoint bytes.Buffer
			if err := f.SaveTo(context.Background(), &checkpoint, &SaveOpts{ExcludeCommittedZeroPages: tc.exclude}); err != nil {
				t.Fatalf("SaveTo: %v", err)
			}
			f.mu.Lock()
			committed := f.knownCommittedBytes
			f.mu.Unlock()
			if got, want := committed, tc.wantCommitted; got != want {
				t.Errorf("knownCommittedBytes: got %d, want %d", got, want)
			}

			restored := newSaveTestMemoryFile(t, tc.diskBacked)
			if err := restored.LoadFrom(context.Background(), &checkpoint, &LoadOpts{}); err != nil {
				t.Fatalf("LoadFrom: %v", err)
			}
			t.Cleanup(func() { restored.DecRef(fr) })
			restored.forEachMappingSlice(fr, func(bs []byte) {
				if !bytes.Equal(bs, want) {
					t.Error("restored contents differ from saved contents")
				}
			})
		})
	}
}

// readFunc lets the restore test intervene after a page reaches its mapping.
type readFunc func([]byte) (int, error)

// Read implements io.Reader.
func (f readFunc) Read(p []byte) (int, error) {
	return f(p)
}

func TestLoadFromWithUpdateUsage(t *testing.T) {
	const page = hostarch.PageSize
	newMemoryFile := func() *MemoryFile {
		t.Helper()
		memfd, err := memutil.CreateMemFD("pgalloc-test", 0)
		if err != nil {
			t.Fatal(err)
		}
		file := os.NewFile(uintptr(memfd), "pgalloc-test")
		f, err := NewMemoryFile(file, MemoryFileOpts{
			DisableMemoryAccounting: true,
		})
		if err != nil {
			_ = file.Close()
			t.Fatal(err)
		}
		t.Cleanup(f.Destroy)
		return f
	}

	ctx := context.Background()
	src := newMemoryFile()
	fr, err := src.Allocate(3*page, AllocOpts{Kind: usage.Anonymous})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { src.DecRef(fr) })

	want := make([]byte, 3*page)
	want[0], want[2*page] = 1, 2
	// The first three-page allocation fits in one chunk.
	src.forEachMappingSlice(fr, func(bs []byte) {
		bs[0], bs[2*page] = want[0], want[2*page]
	})
	var saved bytes.Buffer
	if err := src.SaveTo(ctx, &saved, &SaveOpts{
		ExcludeCommittedZeroPages: true,
	}); err != nil {
		t.Fatal(err)
	}

	data := saved.Bytes()
	metadataEnd := 8 + int(binary.LittleEndian.Uint64(data[:8]))
	probe := bytes.NewReader(data[metadataEnd:])
	length, object, err := state.ReadHeader(&wire.Reader{Reader: probe})
	if err != nil || object || length != page {
		t.Fatalf("first page header: length=%d object=%t err=%v",
			length, object, err)
	}
	firstPayloadEnd := len(data) - probe.Len() + int(length)

	dst := newMemoryFile()
	t.Cleanup(func() {
		// LoadFrom may fail before or after importing this reference.
		dst.mu.Lock()
		seg := dst.unfreeSmall.FindSegment(fr.Start)
		hasRef := seg.Ok() && seg.Value().refs != 0
		dst.mu.Unlock()
		if hasRef {
			dst.DecRef(fr)
		}
	})

	stop := make(chan struct{})
	scanned := make(chan error, 1)
	var scanWG sync.WaitGroup
	scanWG.Go(func() {
		// Observe host commitment without synchronizing with LoadFrom. A
		// signal from the reader would hide missing mapping publication.
		for {
			select {
			case <-stop:
				return
			default:
			}
			committed, err := dst.TotalUsage()
			if err != nil {
				scanned <- err
				return
			}
			if committed >= 2*page {
				scanned <- dst.UpdateUsage(nil)
				return
			}
			runtime.Gosched()
		}
	})
	t.Cleanup(func() {
		close(stop)
		scanWG.Wait()
	})

	input := bytes.NewReader(data)
	updated := false
	reader := readFunc(func(p []byte) (int, error) {
		n, err := input.Read(p)
		if !updated && len(data)-input.Len() >= firstPayloadEnd {
			updated = true
			// Model neighboring-page commitment without requiring THP.
			// This preserves the omitted page's zero contents.
			dst.forEachMappingSlice(memmap.FileRange{
				Start: fr.Start + page,
				End:   fr.Start + 2*page,
			}, func(bs []byte) {
				clear(bs)
			})
			select {
			case err := <-scanned:
				if err != nil {
					t.Fatalf("UpdateUsage during restore: %v", err)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("usage scan did not complete during restore")
			}
		}
		return n, err
	})
	if err := dst.LoadFrom(ctx, reader, &LoadOpts{}); err != nil {
		t.Fatal(err)
	}
	if !updated {
		t.Fatal("restore never reached the accounting update")
	}
	dst.forEachMappingSlice(fr, func(bs []byte) {
		if !bytes.Equal(bs, want) {
			t.Error("restored pages differ from saved pages")
		}
	})
	if input.Len() != 0 {
		t.Errorf("restore left %d bytes unread", input.Len())
	}

	// Count both restored pages and the zero page committed during loading.
	dst.mu.Lock()
	committed := dst.knownCommittedBytes
	dst.mu.Unlock()
	if committed != 3*page {
		t.Errorf("committed bytes after restore: got %d, want %d",
			committed, 3*page)
	}
}
