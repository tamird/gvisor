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

//go:build !pagesize_64k

package pgalloc

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"runtime"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/errors/linuxerr"
	"gvisor.dev/gvisor/pkg/hostarch"
	"gvisor.dev/gvisor/pkg/memutil"
	"gvisor.dev/gvisor/pkg/sentry/memmap"
	"gvisor.dev/gvisor/pkg/sentry/state/stateio"
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
			if got := f.knownCommittedBytes; got != tc.wantCommitted {
				t.Errorf("knownCommittedBytes: got %d, want %d", got, tc.wantCommitted)
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

func TestAsyncSaveToPreservesContents(t *testing.T) {
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

			save := func(exclude bool) ([]byte, []byte) {
				var metadata bytes.Buffer
				var pages bytes.Buffer
				saveDone := make(chan error, 1)
				apfs, err := StartAsyncPagesFileSave(
					stateio.NewIOWriter(&pages, uint64(hostarch.PageSize), 1 /* maxRanges */, 2 /* maxParallel */),
					func(err error) { saveDone <- err },
				)
				if err != nil {
					t.Fatalf("StartAsyncPagesFileSave: %v", err)
				}
				saveErr := f.SaveTo(context.Background(), &metadata, &SaveOpts{
					ExcludeCommittedZeroPages: exclude,
					PagesFile:                 apfs,
				})
				apfs.MemoryFilesDone()
				asyncErr := <-saveDone
				if saveErr != nil {
					t.Fatalf("SaveTo: %v", saveErr)
				}
				if asyncErr != nil {
					t.Fatalf("asynchronous page save: %v", asyncErr)
				}
				if got, want := uint64(pages.Len()), f.knownCommittedBytes; got != want {
					t.Fatalf("pages file length is %d, want %d", got, want)
				}
				return metadata.Bytes(), pages.Bytes()
			}

			save(false)
			pwritePage(t, f, fr, 9, 0)
			pwritePage(t, f, fr, 13, 0)
			want := make([]byte, fr.Length())
			want[2*hostarch.PageSize+17] = 1
			metadata, pages := save(tc.exclude)
			if got := f.knownCommittedBytes; got != tc.wantCommitted {
				t.Errorf("knownCommittedBytes: got %d, want %d", got, tc.wantCommitted)
			}

			restored := newSaveTestMemoryFile(t, tc.diskBacked)
			loadDone := make(chan error, 1)
			apfl, err := StartAsyncPagesFileLoad(
				stateio.NewIOReader(bytes.NewReader(pages), uint64(hostarch.PageSize), 1 /* maxRanges */, 2 /* maxParallel */),
				func(err error) { loadDone <- err },
				nil,
				AsyncPagesFileLoadOpts{},
			)
			if err != nil {
				t.Fatalf("StartAsyncPagesFileLoad: %v", err)
			}
			loadErr := restored.LoadFrom(context.Background(), bytes.NewReader(metadata), &LoadOpts{PagesFile: apfl})
			apfl.MemoryFilesDone()
			asyncErr := <-loadDone
			if loadErr != nil {
				t.Fatalf("LoadFrom: %v", loadErr)
			}
			if asyncErr != nil {
				t.Fatalf("asynchronous page load: %v", asyncErr)
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

// TestSaveToExitAfterSaving checks that SaveTo leaves zero pages that are
// committed on the host in place only if the sentry exits after saving.
func TestSaveToExitAfterSaving(t *testing.T) {
	for _, tc := range []struct {
		name            string
		exitAfterSaving bool
		wantDecommitted bool
	}{
		{name: "resume", wantDecommitted: true},
		{name: "exit after saving", exitAfterSaving: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newSaveTestMemoryFile(t, false)
			fr := allocateSaveTestPages(t, f)
			pwritePage(t, f, fr, 2, 1)
			pwritePage(t, f, fr, 5, 0)
			if err := f.SaveTo(context.Background(), io.Discard, &SaveOpts{ExitAfterSaving: tc.exitAfterSaving}); err != nil {
				t.Fatalf("SaveTo: %v", err)
			}
			if got, want := f.knownCommittedBytes, uint64(hostarch.PageSize); got != want {
				t.Errorf("knownCommittedBytes: got %d, want %d", got, want)
			}
			off := int64(fr.Start) + 5*int64(hostarch.PageSize)
			data, err := unix.Seek(f.FD(), off, unix.SEEK_DATA)
			if err != nil && err != unix.ENXIO {
				t.Fatalf("lseek(SEEK_DATA): %v", err)
			}
			if decommitted := err == unix.ENXIO || data != off; decommitted != tc.wantDecommitted {
				t.Errorf("zero page decommitted: got %t, want %t", decommitted, tc.wantDecommitted)
			}
		})
	}
}

// BenchmarkSaveToCommittedZeroPages measures SaveTo for a 256 MiB MemoryFile
// whose pages are all committed, with one of every 16 pages nonzero.
func BenchmarkSaveToCommittedZeroPages(b *testing.B) {
	const size = 256 << 20
	for _, bc := range []struct {
		name            string
		exitAfterSaving bool
	}{
		{name: "resume"},
		{name: "exit-after-saving", exitAfterSaving: true},
	} {
		b.Run(bc.name, func(b *testing.B) {
			b.SetBytes(size)
			for i := 0; i < b.N; i++ {
				b.StopTimer()
				fd, err := memutil.CreateMemFD("pgalloc-save-bench", 0)
				if err != nil {
					b.Fatalf("CreateMemFD: %v", err)
				}
				f, err := NewMemoryFile(os.NewFile(uintptr(fd), "pgalloc-save-bench"), MemoryFileOpts{
					DelayedEviction:         DelayedEvictionDisabled,
					DisableMemoryAccounting: true,
				})
				if err != nil {
					b.Fatalf("NewMemoryFile: %v", err)
				}
				fr, err := f.Allocate(size, AllocOpts{Mode: AllocateUncommitted})
				if err != nil {
					b.Fatalf("Allocate: %v", err)
				}
				page := 0
				f.forEachMappingSlice(fr, func(bs []byte) {
					for off := 0; off < len(bs); off += hostarch.PageSize {
						if page%16 == 0 {
							bs[off] = 1
						} else {
							bs[off] = 0
						}
						page++
					}
				})
				b.StartTimer()
				if err := f.SaveTo(context.Background(), io.Discard, &SaveOpts{ExitAfterSaving: bc.exitAfterSaving}); err != nil {
					b.Fatalf("SaveTo: %v", err)
				}
				b.StopTimer()
				f.DecRef(fr)
				f.Destroy()
			}
		})
	}
}

// pairedPageReader delivers both page reads together, successful reads first.
// Releasing the results only after MapInternal starts makes the waiter visible
// to the loader irrespective of goroutine scheduling.
type pairedPageReader struct {
	stateio.AsyncReader
	release  chan struct{}
	observed chan []stateio.Completion
}

func (r *pairedPageReader) Wait(cs []stateio.Completion, _ int) ([]stateio.Completion, error) {
	cs, err := r.AsyncReader.Wait(cs, 2)
	if err != nil {
		return cs, err
	}
	if cs[0].N < cs[1].N {
		cs[0], cs[1] = cs[1], cs[0]
	}
	r.observed <- cs
	<-r.release
	return cs, nil
}

func TestAsyncLoadCompletionBatch(t *testing.T) {
	const page = uint64(hostarch.PageSize)
	f := newSaveTestMemoryFile(t, false)
	fr := allocateSaveTestPages(t, f)
	pwritePage(t, f, fr, 0, 1)
	pwritePage(t, f, fr, 1, 2)
	var meta, pages bytes.Buffer
	saved := make(chan error, 1)
	saver, err := StartAsyncPagesFileSave(stateio.NewIOWriter(&pages, page, 1, 2), func(err error) { saved <- err })
	if err != nil {
		t.Fatal(err)
	}
	err = f.SaveTo(context.Background(), &meta, &SaveOpts{PagesFile: saver})
	saver.MemoryFilesDone()
	if err := errors.Join(err, <-saved); err != nil {
		t.Fatal(err)
	}
	if got, want := pages.Len(), int(2*page); got != want {
		t.Fatalf("checkpoint pages size: got %d, want %d", got, want)
	}

	for _, tc := range []struct {
		name    string
		length  int
		wantErr error
	}{
		{"intact", int(2 * page), nil},
		{"truncated", int(page), linuxerr.EIO},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dst := newSaveTestMemoryFile(t, false)
			r := &pairedPageReader{AsyncReader: stateio.NewIOReader(bytes.NewReader(pages.Bytes()[:tc.length]), page, 1, 2), release: make(chan struct{}), observed: make(chan []stateio.Completion, 1)}
			loaded := make(chan error, 1)
			loader, err := StartAsyncPagesFileLoad(r, func(err error) { loaded <- err }, nil, AsyncPagesFileLoadOpts{})
			if err != nil {
				t.Fatal(err)
			}
			err = dst.LoadFrom(context.Background(), bytes.NewReader(meta.Bytes()), &LoadOpts{PagesFile: loader})
			loader.MemoryFilesDone()
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { dst.DecRef(fr) })
			var cs []stateio.Completion
			select {
			case cs = <-r.observed:
			case <-time.After(5 * time.Second):
				t.Fatal("two reads were not submitted")
			}
			t.Logf("page read completions: %d, %d", cs[0].N, cs[1].N)
			if got, want := cs[0].N, page; got != want {
				t.Fatalf("first read: got %d, want %d", got, want)
			}
			if got, want := cs[1].N, uint64(tc.length)-page; got != want {
				t.Fatalf("second read: got %d, want %d", got, want)
			}
			first := memmap.FileRange{Start: fr.Start, End: fr.Start + page}
			mapped := make(chan error, 1)
			go func() { _, err := dst.MapInternal(first, hostarch.Read); mapped <- err }()
			var w *aplWaiter
			for deadline := time.Now().Add(5 * time.Second); w == nil; {
				loader.mu.Lock()
				ul := dst.asyncPageLoad.Load().unloaded.FindSegment(first.Start)
				if len(ul.ValuePtr().waiters) != 0 {
					w = ul.ValuePtr().waiters[0]
				}
				loader.mu.Unlock()
				if time.Now().After(deadline) {
					t.Fatal("MapInternal did not register on the first page")
				}
				runtime.Gosched()
			}
			close(r.release)
			if err := <-loaded; !errors.Is(err, tc.wantErr) {
				t.Fatalf("loader: got %v, want %v", err, tc.wantErr)
			}
			t.Logf("loader finished with %v", tc.wantErr)
			select {
			case err := <-mapped:
				if !errors.Is(err, tc.wantErr) {
					t.Errorf("MapInternal: got %v, want %v", err, tc.wantErr)
				}
			case <-time.After(3 * time.Second):
				w.wakeup.Notify(1)
				<-mapped
				t.Fatal("MapInternal stayed blocked after the page read and loader both finished")
			}
		})
	}
}
