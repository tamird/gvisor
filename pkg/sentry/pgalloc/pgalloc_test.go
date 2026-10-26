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

//go:build !pagesize_64k

package pgalloc

import (
	"bytes"
	"context"
	"encoding/binary"
	"fmt"
	"os"
	"runtime"
	"sync"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/errors/linuxerr"
	"gvisor.dev/gvisor/pkg/hostarch"
	"gvisor.dev/gvisor/pkg/memutil"
	"gvisor.dev/gvisor/pkg/safemem"
	"gvisor.dev/gvisor/pkg/sentry/memmap"
	"gvisor.dev/gvisor/pkg/sentry/usage"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/wire"
)

const (
	page     = hostarch.PageSize
	hugepage = hostarch.HugePageSize
)

func newSaveRestoreMemoryFile(t *testing.T) *MemoryFile {
	t.Helper()
	fd, err := unix.MemfdCreate("pgalloc_save_test", unix.MFD_CLOEXEC)
	if err != nil {
		t.Fatal(err)
	}
	file := os.NewFile(uintptr(fd), "pgalloc_save_test")
	f, err := NewMemoryFile(file, MemoryFileOpts{
		DisableIMAWorkAround:    true,
		DisableMemoryAccounting: true,
		AdviseNoHugepage:        true,
	})
	if err != nil {
		file.Close()
		t.Fatal(err)
	}
	t.Cleanup(f.Destroy)
	return f
}

func TestRetainCommitmentSaveRestore(t *testing.T) {
	zeroPage := make([]byte, page)
	for _, excludeZero := range []bool{false, true} {
		t.Run(fmt.Sprintf("exclude_zero=%t", excludeZero), func(t *testing.T) {
			f := newSaveRestoreMemoryFile(t)
			// Put ordinary zero pages on both sides of retained zero pages. Save
			// must elide only the former, even while updating adjacent metadata.
			var ranges []memmap.FileRange
			for _, retain := range []bool{false, true, false} {
				fr, err := f.Allocate(page, AllocOpts{Mode: AllocateAndCommit, RetainCommitment: retain})
				if err != nil {
					t.Fatal(err)
				}
				allocated := f
				t.Cleanup(func() { allocated.DecRef(fr) })
				ranges = append(ranges, fr)
			}
			// The second round starts with restored, known-committed pages; it
			// checks that the policy survives metadata serialization and also
			// overrides ExcludeCommittedZeroPages on a later checkpoint.
			for round := 0; round < 2; round++ {
				var saved bytes.Buffer
				if err := f.SaveTo(context.Background(), &saved, &SaveOpts{ExcludeCommittedZeroPages: excludeZero}); err != nil {
					t.Fatal(err)
				}
				if got := f.knownCommittedBytes; got != page {
					t.Fatalf("round %d: saved committed bytes = %d, want %d", round, got, page)
				}
				var st unix.Stat_t
				if err := unix.Fstat(f.FD(), &st); err != nil {
					t.Fatal(err)
				}
				if got := uint64(st.Blocks) * 512; got < page {
					t.Fatalf("round %d: live backing after save = %d, want at least %d", round, got, page)
				}
				restored := newSaveRestoreMemoryFile(t)
				if err := restored.LoadFrom(context.Background(), &saved, &LoadOpts{}); err != nil {
					t.Fatal(err)
				}
				for _, fr := range ranges {
					t.Cleanup(func() { restored.DecRef(fr) })
				}
				// Check backing before reading the mapping, which could itself
				// instantiate missing zero pages and conceal a bad restore.
				if err := unix.Fstat(restored.FD(), &st); err != nil {
					t.Fatal(err)
				}
				if got := uint64(st.Blocks) * 512; got != page {
					t.Fatalf("round %d: restored backing = %d, want %d", round, got, page)
				}
				blocks, err := restored.MapInternal(ranges[1], hostarch.ReadWrite)
				if err != nil {
					t.Fatal(err)
				}
				for bs := blocks; !bs.IsEmpty(); bs = bs.Tail() {
					if !bytes.Equal(bs.Head().ToSlice(), zeroPage[:bs.Head().Len()]) {
						t.Fatalf("round %d: retained page did not restore zero bytes", round)
					}
				}
				f = restored
			}
		})
	}
}

func TestRetainCommitmentDecommit(t *testing.T) {
	f := newSaveRestoreMemoryFile(t)
	ordinary, err := f.Allocate(page, AllocOpts{Mode: AllocateAndCommit})
	if err != nil {
		t.Fatal(err)
	}
	defer f.DecRef(ordinary)
	retained, err := f.Allocate(page, AllocOpts{Mode: AllocateAndCommit, RetainCommitment: true})
	if err != nil {
		t.Fatal(err)
	}
	defer f.DecRef(retained)
	if ordinary.End != retained.Start {
		t.Fatal("expected adjacent allocations")
	}
	blocks, err := f.MapInternal(ordinary, hostarch.ReadWrite)
	if err != nil {
		t.Fatal(err)
	}
	blocks.Head().ToSlice()[0] = 1
	func() {
		defer func() {
			if recover() == nil {
				t.Error("Decommit accepted a range containing retained commitment")
			}
		}()
		f.Decommit(memmap.FileRange{ordinary.Start, retained.End})
	}()
	if got := blocks.Head().ToSlice()[0]; got != 1 {
		t.Fatalf("rejected Decommit changed the ordinary prefix to %d", got)
	}
	f.Decommit(ordinary)
	if got := blocks.Head().ToSlice()[0]; got != 0 {
		t.Fatalf("ordinary Decommit left byte %d, want zero", got)
	}
}

func TestRetainCommitmentReferences(t *testing.T) {
	// Exercise the range metadata without a releaser racing to consume waste,
	// as in TestFindAllocatable. No backing pages are accessed here.
	f := &MemoryFile{opts: MemoryFileOpts{DisableMemoryAccounting: true}}
	f.initFields()
	chunks := []chunkInfo{{}}
	f.chunks.Store(&chunks)
	fr := memmap.FileRange{0, 2 * page}
	f.unfreeSmall.RemoveRange(fr)
	alloc := allocState{
		length:     fr.Length(),
		opts:       AllocOpts{Mode: AllocateAndCommit, RetainCommitment: true},
		willCommit: true,
	}
	if got, err := f.findAllocatableAndMarkUsed(&alloc); err != nil || got != fr {
		t.Fatalf("allocation = (%v, %v), want (%v, nil)", got, err, fr)
	}
	first := memmap.FileRange{0, page}
	f.IncRef(first, 0)
	f.DecRef(fr)
	f.mu.Lock()
	for _, want := range []struct {
		offset uint64
		retain bool
	}{{0, true}, {page, false}} {
		if got := f.memAcct.FindSegment(want.offset).Value().retainCommitment; got != want.retain {
			t.Errorf("retainCommitment at %d = %t, want %t after partial release", want.offset, got, want.retain)
		}
	}
	f.mu.Unlock()
	f.DecRef(first)
	// Recycle the same range into an ordinary allocation. A stale retained
	// policy would defeat zero-page elision for the new owner.
	alloc.opts.RetainCommitment = false
	alloc.recycled = false
	if got, err := f.findAllocatableAndMarkUsed(&alloc); err != nil || got != fr || !alloc.recycled {
		t.Fatalf("recycled allocation = (%v, %v), recycled=%t; want (%v, nil), true", got, err, alloc.recycled, fr)
	}
	f.mu.Lock()
	f.memAcct.VisitFullRange(fr, func(seg memAcctIterator) bool {
		if seg.Value().retainCommitment {
			t.Errorf("recycled ordinary range %v retained the prior allocation's policy", seg.Range())
		}
		return true
	})
	f.mu.Unlock()
	f.DecRef(fr)
	alloc.opts.RetainCommitment = true
	if got, err := f.findAllocatableAndMarkUsed(&alloc); err != nil || got != fr {
		t.Fatalf("retained reuse = (%v, %v), want (%v, nil)", got, err, fr)
	}
	f.mu.Lock()
	f.memAcct.VisitFullRange(fr, func(seg memAcctIterator) bool {
		if !seg.Value().retainCommitment {
			t.Errorf("recycled retained range %v lost its new allocation policy", seg.Range())
		}
		return true
	})
	f.mu.Unlock()
	f.DecRef(fr)
}

func TestAllocateAndCommit(t *testing.T) {
	for _, test := range []struct {
		name        string
		fr          memmap.FileRange
		fileSize    int64
		recycled    bool
		huge        bool
		wantFault   bool
		retryLength uint64
	}{
		{
			name: "small advised allocation", fr: memmap.FileRange{0, page}, fileSize: page,
		},
		{
			name: "recycled allocation", fr: memmap.FileRange{0, page}, fileSize: page, recycled: true,
		},
		{
			name: "recycled fault", fr: memmap.FileRange{0, page}, recycled: true, wantFault: true,
		},
		{
			name: "fault across hugepage boundary", fr: memmap.FileRange{hugepage - page, hugepage + page}, fileSize: hugepage, wantFault: true,
		},
		{
			name: "huge advised fault", fr: memmap.FileRange{0, hugepage}, huge: true, wantFault: true,
		},
		{
			name: "commit after backing fault", fr: memmap.FileRange{0, 2 * page}, wantFault: true, retryLength: 3 * page,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			fd, err := unix.MemfdCreate("pgalloc_test", unix.MFD_CLOEXEC)
			if err != nil {
				t.Fatal(err)
			}
			file := os.NewFile(uintptr(fd), "pgalloc_test")
			defer file.Close()
			if err := file.Truncate(test.fileSize); err != nil {
				t.Fatal(err)
			}
			available := memmap.FileRange{test.fr.Start, test.fr.End + test.retryLength}
			mapping, err := unix.Mmap(int(file.Fd()), 0, int(available.End), unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
			if err != nil {
				t.Fatal(err)
			}
			defer unix.Munmap(mapping)
			if test.recycled && !test.wantFault {
				for i := range mapping {
					mapping[i] = 0xff
				}
			}

			// Expose only this allocation, without a releaser racing to reclaim
			// waste pages. Mapping past EOF supplies a real, bounded SIGBUS.
			f := &MemoryFile{
				file: file,
				opts: MemoryFileOpts{
					DisableMemoryAccounting: true,
					ExpectHugepages:         test.huge,
					AdviseHugepage:          test.huge,
					AdviseNoHugepage:        !test.huge,
				},
			}
			f.initFields()
			chunks := []chunkInfo{{mapping: uintptr(unsafe.Pointer(&mapping[0])), huge: test.huge}}
			f.chunks.Store(&chunks)
			f.madviseChunkMapping(chunks[0].mapping, uintptr(len(mapping)), test.huge)
			unfree, unwaste := &f.unfreeSmall, &f.unwasteSmall
			if test.huge {
				unfree, unwaste = &f.unfreeHuge, &f.unwasteHuge
			}
			if test.recycled {
				unwaste.RemoveRange(test.fr)
				f.memAcct.InsertRange(test.fr, memAcctInfo{wasteOrReleasing: true})
			} else {
				unfree.RemoveRange(available)
			}

			readerCalled := false
			opts := AllocOpts{
				Mode: AllocateAndCommit,
				Huge: test.huge,
				ReaderFunc: func(dsts safemem.BlockSeq) (uint64, error) {
					readerCalled = true
					var st unix.Stat_t
					if err := unix.Fstat(int(file.Fd()), &st); err != nil {
						t.Fatal(err)
					}
					if got := uint64(st.Blocks) * 512; got < test.fr.Length() {
						t.Errorf("committed bytes before ReaderFunc = %d, want at least %d", got, test.fr.Length())
					}
					for blocks := dsts; !blocks.IsEmpty(); blocks = blocks.Tail() {
						for i, b := range blocks.Head().ToSlice() {
							if b != 0 {
								t.Fatalf("allocated byte %d = %#x, want zero", i, b)
							}
						}
					}
					return safemem.ZeroSeq(dsts)
				},
			}
			fr, err := f.Allocate(test.fr.Length(), opts)
			if test.wantFault {
				if !linuxerr.Equals(linuxerr.ENOMEM, err) {
					t.Fatalf("Allocate() = (%v, %v), want ENOMEM", fr, err)
				}
				if fr != (memmap.FileRange{}) || readerCalled {
					t.Errorf("failed Allocate() returned %v, called reader=%t", fr, readerCalled)
				}
				unfree.VisitFullRange(test.fr, func(seg unfreeIterator) bool {
					if got := seg.Value().refs; got != 0 {
						t.Errorf("failed allocation retained %d references on %v", got, seg.Range())
					}
					return true
				})
				if test.retryLength == 0 {
					return
				}
				// The failed population above makes the allocator use its
				// fallback. A larger retry cannot recycle the failed range, so
				// its sparse policy touches still require full commitment.
				if err := file.Truncate(int64(available.End)); err != nil {
					t.Fatal(err)
				}
				test.fr = memmap.FileRange{test.fr.End, available.End}
				fr, err = f.Allocate(test.retryLength, opts)
			}
			if err != nil || fr != test.fr || !readerCalled {
				t.Fatalf("Allocate() = (%v, %v), called reader=%t; want (%v, nil), true", fr, err, readerCalled, test.fr)
			}
			f.DecRef(fr)
		})
	}
}

// existingSegment represents a range of pages in a test MemoryFile that is not
// void or free.
type existingSegment struct {
	start uint64
	end   uint64
	state int
}

// Possible values for existingSegment.state:
const (
	existingUnspecified = iota
	existingUsed
	existingWaste
	existingReleasing // or sub-releasing
)

func TestFindAllocatable(t *testing.T) {
	for _, test := range []struct {
		name string
		// Initial state:
		chunkHuge []bool
		existing  []existingSegment
		// Allocation parameters:
		length  uint64
		huge    bool
		recycle bool
		dir     Direction
		// Expected outcome:
		want uint64
	}{
		{
			name:   "initial small allocation, bottom-up",
			length: page,
			want:   0,
		},
		{
			name:   "initial small allocation, top-down",
			length: page,
			dir:    TopDown,
			want:   chunkSize - page,
		},
		{
			name:   "initial small allocation, multiple pages, top-down",
			length: 2 * page,
			dir:    TopDown,
			want:   chunkSize - 2*page,
		},
		{
			name:    "initial small allocation, recycling enabled, bottom-up",
			length:  page,
			recycle: true,
			want:    0,
		},
		{
			name:   "initial huge allocation, bottom-up",
			length: hugepage,
			huge:   true,
			want:   0,
		},
		{
			name:   "initial huge allocation, top-down",
			length: hugepage,
			huge:   true,
			dir:    TopDown,
			want:   chunkSize - hugepage,
		},
		{
			name:   "initial huge allocation, multiple pages, top-down",
			length: 2 * hugepage,
			huge:   true,
			dir:    TopDown,
			want:   chunkSize - 2*hugepage,
		},
		{
			name:    "initial huge allocation, recycling enabled, bottom-up",
			length:  hugepage,
			huge:    true,
			recycle: true,
			want:    0,
		},
		{
			name:      "huge allocation uses huge pages in new chunk",
			chunkHuge: []bool{false},
			length:    hugepage,
			huge:      true,
			want:      chunkSize,
		},
		{
			name:      "huge allocation uses huge pages in existing chunk",
			chunkHuge: []bool{false, true},
			length:    hugepage,
			huge:      true,
			want:      chunkSize,
		},
		{
			name:      "hugepage-sized non-huge allocation uses small pages in new chunk",
			chunkHuge: []bool{true},
			length:    hugepage,
			want:      chunkSize,
		},
		{
			name:      "hugepage-sized non-huge allocation uses small pages in existing chunk",
			chunkHuge: []bool{true, false},
			length:    hugepage,
			want:      chunkSize,
		},
		{
			name:      "bottom-up small allocation begins at start of file",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{page, 2 * page, existingUsed},
			},
			length: page,
			want:   0,
		},
		{
			name:      "top-down small allocation begins at end of last chunk",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{chunkSize - 2*page, chunkSize - page, existingUsed},
			},
			length: page,
			dir:    TopDown,
			want:   chunkSize - page,
		},
		{
			name:      "bottom-up huge allocation begins at start of file",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{hugepage, 2 * hugepage, existingUsed},
			},
			length: hugepage,
			huge:   true,
			want:   0,
		},
		{
			name:      "top-down huge allocation begins at end of last chunk",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{chunkSize - 2*hugepage, chunkSize - hugepage, existingUsed},
			},
			length: hugepage,
			huge:   true,
			dir:    TopDown,
			want:   chunkSize - hugepage,
		},
		{
			name:      "bottom-up small allocation can extend multiple chunks",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{chunkSize/2 - page, chunkSize / 2, existingUsed},
			},
			length: 2*chunkSize + page,
			want:   chunkSize / 2,
		},
		{
			name:      "top-down small allocation can extend multiple chunks",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{chunkSize/2 - page, chunkSize / 2, existingUsed},
			},
			length: 2*chunkSize + page,
			dir:    TopDown,
			want:   chunkSize - page,
		},
		{
			name:      "bottom-up huge allocation can extend multiple chunks",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{chunkSize/2 - hugepage, chunkSize / 2, existingUsed},
			},
			length: 2*chunkSize + hugepage,
			huge:   true,
			want:   chunkSize / 2,
		},
		{
			name:      "top-down huge allocation can extend multiple chunks",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{chunkSize/2 - hugepage, chunkSize / 2, existingUsed},
			},
			length: 2*chunkSize + hugepage,
			huge:   true,
			dir:    TopDown,
			want:   chunkSize - hugepage,
		},
		{
			name:      "bottom-up small allocation finds first free gap",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{0, page, existingUsed},
				{2 * page, 3 * page, existingUsed},
			},
			length: page,
			want:   page,
		},
		{
			name:      "top-down small allocation finds last free gap",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{chunkSize - page, chunkSize, existingUsed},
				{chunkSize - 3*page, chunkSize - 2*page, existingUsed},
			},
			length: page,
			dir:    TopDown,
			want:   chunkSize - 2*page,
		},
		{
			name:      "bottom-up huge allocation finds first free gap",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{0, hugepage, existingUsed},
				{2 * hugepage, 3 * hugepage, existingUsed},
			},
			length: hugepage,
			huge:   true,
			want:   hugepage,
		},
		{
			name:      "top-down huge allocation finds last free gap",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{chunkSize - hugepage, chunkSize, existingUsed},
				{chunkSize - 3*hugepage, chunkSize - 2*hugepage, existingUsed},
			},
			length: hugepage,
			huge:   true,
			dir:    TopDown,
			want:   chunkSize - 2*hugepage,
		},
		{
			name:      "bottom-up small allocation skips undersized free gap",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{0, page, existingUsed},
				{2 * page, 3 * page, existingUsed},
			},
			length: 2 * page,
			want:   3 * page,
		},
		{
			name:      "top-down small allocation skips undersized free gap",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{chunkSize - page, chunkSize, existingUsed},
				{chunkSize - 3*page, chunkSize - 2*page, existingUsed},
			},
			length: 2 * page,
			dir:    TopDown,
			want:   chunkSize - 5*page,
		},
		{
			name:      "bottom-up huge allocation skips undersized free gap",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{0, hugepage, existingUsed},
				{2 * hugepage, 3 * hugepage, existingUsed},
			},
			length: 2 * hugepage,
			huge:   true,
			want:   3 * hugepage,
		},
		{
			name:      "top-down huge allocation skips undersized free gap",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{chunkSize - hugepage, chunkSize, existingUsed},
				{chunkSize - 3*hugepage, chunkSize - 2*hugepage, existingUsed},
			},
			length: 2 * hugepage,
			huge:   true,
			dir:    TopDown,
			want:   chunkSize - 5*hugepage,
		},
		{
			name:      "recycling bottom-up small allocation skips used pages",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{0, page, existingUsed},
			},
			length:  page,
			recycle: true,
			want:    page,
		},
		{
			name:      "recycling top-down small allocation skips used pages",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{chunkSize - page, chunkSize, existingUsed},
			},
			length:  page,
			recycle: true,
			dir:     TopDown,
			want:    chunkSize - 2*page,
		},
		{
			name:      "recycling bottom-up huge allocation skips used pages",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{0, hugepage, existingUsed},
			},
			length:  hugepage,
			huge:    true,
			recycle: true,
			want:    hugepage,
		},
		{
			name:      "recycling top-down huge allocation skips used pages",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{chunkSize - hugepage, chunkSize, existingUsed},
			},
			length:  hugepage,
			huge:    true,
			recycle: true,
			dir:     TopDown,
			want:    chunkSize - 2*hugepage,
		},
		{
			name:      "non-recycling bottom-up small allocation skips waste pages",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{0, page, existingWaste},
			},
			length: page,
			want:   page,
		},
		{
			name:      "non-recycling top-down small allocation skips waste pages",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{chunkSize - page, chunkSize, existingWaste},
			},
			length: page,
			dir:    TopDown,
			want:   chunkSize - 2*page,
		},
		{
			name:      "non-recycling bottom-up huge allocation skips waste pages",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{0, hugepage, existingWaste},
			},
			length: hugepage,
			huge:   true,
			want:   hugepage,
		},
		{
			name:      "non-recycling top-down huge allocation skips waste pages",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{chunkSize - hugepage, chunkSize, existingWaste},
			},
			length: hugepage,
			huge:   true,
			dir:    TopDown,
			want:   chunkSize - 2*hugepage,
		},
		{
			name:      "recycling bottom-up small allocation recycles waste pages",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{0, page, existingWaste},
			},
			length:  page,
			recycle: true,
			want:    0,
		},
		{
			name:      "recycling top-down small allocation recycles waste pages",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{chunkSize - page, chunkSize, existingWaste},
			},
			length:  page,
			recycle: true,
			dir:     TopDown,
			want:    chunkSize - page,
		},
		{
			name:      "recycling bottom-up huge allocation recycles waste pages",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{0, hugepage, existingWaste},
			},
			length:  hugepage,
			huge:    true,
			recycle: true,
			want:    0,
		},
		{
			name:      "recycling top-down huge allocation recycles waste pages",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{chunkSize - hugepage, chunkSize, existingWaste},
			},
			length:  hugepage,
			huge:    true,
			recycle: true,
			dir:     TopDown,
			want:    chunkSize - hugepage,
		},
		{
			name:      "non-recycling bottom-up small allocation skips releasing pages",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{0, page, existingReleasing},
			},
			length: page,
			want:   page,
		},
		{
			name:      "non-recycling top-down small allocation skips releasing pages",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{chunkSize - page, chunkSize, existingReleasing},
			},
			length: page,
			dir:    TopDown,
			want:   chunkSize - 2*page,
		},
		{
			name:      "non-recycling bottom-up huge allocation skips releasing pages",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{0, hugepage, existingReleasing},
			},
			length: hugepage,
			huge:   true,
			want:   hugepage,
		},
		{
			name:      "non-recycling top-down huge allocation skips releasing pages",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{chunkSize - hugepage, chunkSize, existingReleasing},
			},
			length: hugepage,
			huge:   true,
			dir:    TopDown,
			want:   chunkSize - 2*hugepage,
		},
		{
			name:      "recycling bottom-up small allocation skips releasing pages",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{0, page, existingReleasing},
			},
			length:  page,
			recycle: true,
			want:    page,
		},
		{
			name:      "recycling top-down small allocation skips releasing pages",
			chunkHuge: []bool{false},
			existing: []existingSegment{
				{chunkSize - page, chunkSize, existingReleasing},
			},
			length:  page,
			recycle: true,
			dir:     TopDown,
			want:    chunkSize - 2*page,
		},
		{
			name:      "recycling bottom-up huge allocation skips releasing pages",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{0, hugepage, existingReleasing},
			},
			length:  hugepage,
			huge:    true,
			recycle: true,
			want:    hugepage,
		},
		{
			name:      "recycling top-down huge allocation skips releasing pages",
			chunkHuge: []bool{true},
			existing: []existingSegment{
				{chunkSize - hugepage, chunkSize, existingReleasing},
			},
			length:  hugepage,
			huge:    true,
			recycle: true,
			dir:     TopDown,
			want:    chunkSize - 2*hugepage,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			// Build the fake MemoryFile.
			f := &MemoryFile{
				opts: MemoryFileOpts{
					ExpectHugepages:         true,
					DisableMemoryAccounting: true,
				},
			}
			// f is a local fixture with no releaser or published references.
			// checklocks cannot substitute this ownership for the mutex contract.
			f.initFields() // +checklocksignore
			chunks := make([]chunkInfo, len(test.chunkHuge))
			for i, huge := range test.chunkHuge {
				chunks[i].huge = huge
				chunkFR := memmap.FileRange{uint64(i) * chunkSize, uint64(i+1) * chunkSize}
				if huge {
					f.unfreeHuge.RemoveRange(chunkFR) // +checklocksignore
				} else {
					f.unfreeSmall.RemoveRange(chunkFR) // +checklocksignore
				}
			}
			// f remains local to this fixture; checklocks does not track that
			// ownership through initFields.
			f.chunks.Store(&chunks) // +checklocksignore
			// The synchronous callbacks below only initialize this private
			// fixture; checklocks cannot track that ownership into them.
			for _, es := range test.existing {
				f.forEachChunk(memmap.FileRange{es.start, es.end}, func(chunk *chunkInfo, chunkFR memmap.FileRange) bool {
					unwaste, unfree := &f.unwasteSmall, &f.unfreeSmall // +checklocksignore
					if chunk.huge {
						unwaste, unfree = &f.unwasteHuge, &f.unfreeHuge // +checklocksignore
					}
					switch es.state {
					case existingUsed:
						unfree.InsertRange(chunkFR, unfreeInfo{refs: 1})
					case existingWaste:
						unfree.InsertRange(chunkFR, unfreeInfo{refs: 0})
						unwaste.RemoveRange(chunkFR)
					case existingReleasing:
						unfree.InsertRange(chunkFR, unfreeInfo{refs: 0})
					default:
						t.Fatalf("existingSegment %+v has unknown state", es)
					}
					f.memAcct.InsertRange(chunkFR, memAcctInfo{ // +checklocksignore
						wasteOrReleasing: es.state != existingUsed,
					})
					return true
				})
			}

			// Perform the test allocation.
			alloc := allocState{
				length: test.length,
				opts: AllocOpts{
					Huge: test.huge,
					Dir:  test.dir,
				},
				huge: test.huge,
			}
			if test.recycle {
				alloc.opts.Mode = AllocateCallerIndirectCommit
				alloc.willCommit = true
			}
			fr, err := f.findAllocatableAndMarkUsed(&alloc)
			if err != nil {
				t.Fatalf("findAllocatableAndMarkUsed(%+v): failed: %v, want: %#x\n%v", alloc, err, test.want, f)
			}
			if fr.Start != test.want {
				t.Errorf("findAllocatableAndMarkUsed(%+v): got: start=%#x, want: %#x\n%v", alloc, fr.Start, test.want, f)
			}
			if wantEnd := test.want + test.length; fr.End != wantEnd {
				t.Errorf("findAllocatableAndMarkUsed(%+v): got: end=%#x, want: %#x\n%v", alloc, fr.End, wantEnd, f)
			}
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
