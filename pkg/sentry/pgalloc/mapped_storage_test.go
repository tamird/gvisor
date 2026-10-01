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
	"testing"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/sentry/memmap"
	"gvisor.dev/gvisor/pkg/sentry/state/stateio"
	"gvisor.dev/gvisor/pkg/sentry/usage"
	"gvisor.dev/gvisor/pkg/state"
)

func checkMappedReferences(t *testing.T, f *MemoryFile, fr memmap.FileRange, want uint64) {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	for offset := fr.Start; offset < fr.End; offset += page {
		var got uint64
		if seg := f.unfreeSmall.FindSegment(offset); seg.Ok() {
			got = seg.Value().refs
		}
		if got != want {
			t.Errorf("page %#x references = %d, want %d", offset, got, want)
		}
	}
}

func TestMappedStorageMappingBoundary(t *testing.T) {
	f := newSaveRestoreMemoryFile(t)
	// Reserve virtual space without committing its contents, so a three-page
	// allocation crosses the actual MemoryFile mapping boundary. Only those
	// three pages are backed or touched.
	prefix, err := f.Allocate(chunkSize-page, AllocOpts{})
	if err != nil {
		t.Fatal(err)
	}
	defer f.DecRef(prefix)
	storage, err := f.AllocateMapped(2*page+37, 2*page, usage.System, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		for _, s := range storage {
			s.Release()
		}
	}()
	if len(storage) != 2 {
		t.Fatalf("got %d storage pieces, want 2", len(storage))
	}
	for i, want := range []struct {
		fr     memmap.FileRange
		length int
	}{
		{memmap.FileRange{Start: chunkSize - page, End: chunkSize}, page},
		{memmap.FileRange{Start: chunkSize, End: chunkSize + 2*page}, page + 37},
	} {
		s := storage[i]
		if s.fr != want.fr || len(s.Bytes()) != want.length || cap(s.Bytes()) != want.length {
			t.Fatalf("piece %d = %v, len=%d cap=%d; want %v, len=cap=%d", i, s.fr, len(s.Bytes()), cap(s.Bytes()), want.fr, want.length)
		}
		checkMappedReferences(t, f, s.fr, 1)
		for j := range s.Bytes() {
			s.Bytes()[j] = byte(i + 1)
		}
	}
}

// blockedPageReader lets the real asynchronous loader start before page I/O can
// complete. Failure uses io.ReaderAt's real short-read contract, not a synthetic
// MapInternal error or a replacement restore implementation.
type blockedPageReader struct {
	*bytes.Reader
	ready chan struct{}
	read  chan struct{}
}

func (r *blockedPageReader) ReadAt(dst []byte, off int64) (int, error) {
	select {
	case r.read <- struct{}{}:
	default:
	}
	<-r.ready
	return r.Reader.ReadAt(dst, off)
}

func TestMappedStorageSaveRestore(t *testing.T) {
	for _, mode := range []string{"sync", "async", "async error", "cold cleanup"} {
		t.Run(mode, func(t *testing.T) {
			f := newSaveRestoreMemoryFile(t)
			storage, err := f.AllocateMapped(2*page+37, page, usage.System, 0)
			if err != nil {
				t.Fatal(err)
			}
			var ranges []memmap.FileRange
			var original buffer.Buffer
			defer original.Release()
			for i, s := range storage {
				ranges = append(ranges, s.fr)
				// Keep a complete zero page: restore must retain its backing
				// even with committed zero-page exclusion enabled.
				if i != 1 {
					for j := range s.Bytes() {
						s.Bytes()[j] = byte(i + 1)
					}
				}
				if err := original.Append(buffer.NewViewWithExternalStorage(s)); err != nil {
					t.Fatal(err)
				}
			}
			if err := original.Append(buffer.NewViewWithData([]byte("heap tail"))); err != nil {
				t.Fatal(err)
			}
			original.TrimFront(3)
			shared := original.Clone()
			defer shared.Release()
			graphs := []*buffer.Buffer{&original, &shared}
			want := original.Flatten()
			// Two checkpoints include the original post-save resume and a
			// second checkpoint of a restored owner graph.
			for round := 0; round < 2; round++ {
				var graph, metadata, pages bytes.Buffer
				ctx := context.Background()
				if _, err := state.Save(ctx, &graph, &graphs); err != nil {
					t.Fatal(err)
				}
				saveOpts := SaveOpts{ExcludeCommittedZeroPages: true}
				async := mode == "async" || mode == "async error"
				if async {
					done := make(chan error, 1)
					apfs, err := StartAsyncPagesFileSave(stateio.NewIOWriter(&pages, 4*page, 4, 1), func(err error) { done <- err })
					if err != nil {
						t.Fatal(err)
					}
					saveOpts.PagesFile = apfs
					err = f.SaveTo(ctx, &metadata, &saveOpts)
					apfs.MemoryFilesDone()
					if doneErr := <-done; err != nil || doneErr != nil {
						t.Fatalf("SaveTo = %v, async completion = %v", err, doneErr)
					}
				} else if err := f.SaveTo(ctx, &metadata, &saveOpts); err != nil {
					t.Fatal(err)
				}
				if got := f.knownCommittedBytes; got != 3*page {
					t.Fatalf("round %d: saved %d committed bytes, want %d", round, got, 3*page)
				}
				// Saving must leave the original live mapping usable.
				if got := graphs[0].Flatten(); !bytes.Equal(got, want) {
					t.Fatal("save changed live payload")
				}
				for _, b := range graphs {
					b.Release()
				}
				for _, fr := range ranges {
					checkMappedReferences(t, f, fr, 0)
				}

				restoredMF := newSaveRestoreMemoryFile(t)
				var restore MappedStorageRestore
				ctx = context.WithValue(ctx, CtxMemoryFile, restoredMF)
				if mode != "cold cleanup" {
					ctx = context.WithValue(ctx, CtxMappedStorageRestore, &restore)
				}
				var restored []*buffer.Buffer
				if _, err := state.Load(ctx, &graph, &restored); err != nil {
					t.Fatal(err)
				}
				if len(restored) != 2 {
					t.Fatalf("restored %d buffers, want 2", len(restored))
				}
				// Register cleanup before checking the graph. It remains valid
				// without Bytes after MF metadata has loaded, including errors.
				for _, b := range restored {
					t.Cleanup(b.Release)
				}
				if mode != "cold cleanup" && len(restore.storage) != 3 {
					t.Fatalf("registered %d owners, want 3 shared across both buffers", len(restore.storage))
				}
				owners := append([]*MappedStorage(nil), restore.storage...)
				for _, s := range owners {
					if s.data != nil {
						t.Fatal("state.Load exposed bytes before MemoryFile loading")
					}
				}
				var loadDone chan error
				var reader *blockedPageReader
				loadOpts := LoadOpts{}
				if async {
					pageData := pages.Bytes()
					if mode == "async error" {
						pageData = nil
					}
					reader = &blockedPageReader{Reader: bytes.NewReader(pageData), ready: make(chan struct{}), read: make(chan struct{}, 1)}
					loadDone = make(chan error, 1)
					apfl, err := StartAsyncPagesFileLoad(stateio.NewIOReader(reader, 4*page, 4, 1), func(err error) { loadDone <- err }, nil)
					if err != nil {
						t.Fatal(err)
					}
					loadOpts.PagesFile = apfl
					err = restoredMF.LoadFrom(ctx, &metadata, &loadOpts)
					apfl.MemoryFilesDone()
					if err != nil {
						close(reader.ready)
						<-loadDone
						t.Fatal(err)
					}
					<-reader.read
				} else if err := restoredMF.LoadFrom(ctx, &metadata, &loadOpts); err != nil {
					t.Fatal(err)
				}
				if mode == "cold cleanup" {
					// Filesystem-only extraction does not need packet mappings.
					for _, b := range restored {
						b.Release()
					}
					for _, fr := range ranges {
						checkMappedReferences(t, restoredMF, fr, 0)
					}
					return
				}
				var restoreErr error
				if async {
					result := make(chan error, 1)
					go func() { result <- restore.Restore() }()
					close(reader.ready)
					restoreErr = <-result
					loadErr := <-loadDone
					if mode == "async error" {
						if restoreErr == nil || loadErr == nil {
							t.Fatalf("truncated page file: restore=%v, loader=%v; want both errors", restoreErr, loadErr)
						}
						for _, s := range owners {
							if s.data != nil {
								t.Error("failed loading exposed bytes")
							}
						}
						for _, b := range restored {
							b.Release()
						}
						for _, fr := range ranges {
							checkMappedReferences(t, restoredMF, fr, 0)
						}
						if len(restore.storage) != 0 {
							t.Error("failed barrier retained borrowed owners")
						}
						return
					}
					if loadErr != nil {
						t.Fatal(loadErr)
					}
				} else {
					restoreErr = restore.Restore()
				}
				if restoreErr != nil {
					t.Fatal(restoreErr)
				}
				if len(restore.storage) != 0 {
					t.Error("successful barrier retained borrowed owners")
				}
				// Measure before Bytes access can fault in missing zero pages.
				var st unix.Stat_t
				if err := unix.Fstat(restoredMF.FD(), &st); err != nil {
					t.Fatal(err)
				}
				if got := st.Blocks * 512; got != 3*page {
					t.Fatalf("restored backing = %d, want %d", got, 3*page)
				}
				for _, b := range restored {
					if got := b.Flatten(); !bytes.Equal(got, want) {
						t.Fatalf("round %d: restored payload differs", round)
					}
				}
				for _, fr := range ranges {
					checkMappedReferences(t, restoredMF, fr, 1)
				}
				graphs, f = restored, restoredMF
			}
			// COW after the second real restore must preserve the other buffer,
			// whose chunk reference still owns the mapped pages.
			data, ok := graphs[0].PullUp(0, 1)
			if !ok {
				t.Fatal("PullUp failed")
			}
			data.AsSlice()[0] ^= 0xff
			if got := graphs[1].Flatten(); !bytes.Equal(got, want) {
				t.Fatal("restored COW changed shared buffer")
			}
			graphs[0].Release()
			for _, fr := range ranges {
				checkMappedReferences(t, f, fr, 1)
			}
			graphs[1].Release()
			for _, fr := range ranges {
				checkMappedReferences(t, f, fr, 0)
			}
		})
	}
}
