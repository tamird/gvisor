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
	"os"
	"testing"
	"unsafe"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/sentry/memmap"
)

// BenchmarkAllocateRecycled measures Allocate() calls that recycle committed
// waste pages, as mm does for private anonymous page faults
// (AllocateCallerIndirectCommit). In "hot" cases the same range is recycled
// on every iteration; in "cold" cases a ring of ranges spanning 256 MiB is
// recycled, so the zeroed pages are not cache-resident.
func BenchmarkAllocateRecycled(b *testing.B) {
	for _, bc := range []struct {
		name string
		size uint64
		ring int
	}{
		{"4K/hot", page, 1},
		{"64K/hot", 16 * page, 1},
		{"2M/hot", hugepage, 1},
		{"4K/cold", page, (256 << 20) / page},
		{"64K/cold", 16 * page, (256 << 20) / (16 * page)},
		{"2M/cold", hugepage, (256 << 20) / hugepage},
	} {
		b.Run(bc.name, func(b *testing.B) {
			total := bc.size * uint64(bc.ring)
			fd, err := unix.MemfdCreate("pgalloc_bench", unix.MFD_CLOEXEC)
			if err != nil {
				b.Fatal(err)
			}
			file := os.NewFile(uintptr(fd), "pgalloc_bench")
			defer file.Close()
			if err := file.Truncate(int64(total)); err != nil {
				b.Fatal(err)
			}
			mapping, err := unix.Mmap(int(file.Fd()), 0, int(total), unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
			if err != nil {
				b.Fatal(err)
			}
			defer unix.Munmap(mapping)
			// Commit all pages so that recycling never faults.
			for i := range mapping {
				mapping[i] = 0xff
			}
			// No releaser goroutine, so waste pages stay recyclable.
			f := &MemoryFile{
				file: file,
				opts: MemoryFileOpts{DisableMemoryAccounting: true},
			}
			f.initFields()
			chunks := []chunkInfo{{mapping: uintptr(unsafe.Pointer(&mapping[0]))}}
			f.chunks.Store(&chunks)
			all := memmap.FileRange{0, total}
			f.unwasteSmall.RemoveRange(all)
			f.memAcct.InsertRange(all, memAcctInfo{wasteOrReleasing: true})
			opts := AllocOpts{Mode: AllocateCallerIndirectCommit}
			ring := make([]memmap.FileRange, bc.ring)
			for j := range ring {
				if ring[j], err = f.Allocate(bc.size, opts); err != nil {
					b.Fatal(err)
				}
			}
			b.SetBytes(int64(bc.size))
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				j := i % bc.ring
				// ring[j] becomes the only waste range, so it is recycled.
				f.DecRef(ring[j])
				if ring[j], err = f.Allocate(bc.size, opts); err != nil {
					b.Fatal(err)
				}
			}
			b.StopTimer()
			for _, fr := range ring {
				f.DecRef(fr)
			}
		})
	}
}
