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
	"gvisor.dev/gvisor/pkg/hostarch"
	"gvisor.dev/gvisor/pkg/sentry/memmap"
)

// BenchmarkSparseSharedCheckpoint holds two references to every fourth page.
// Save and load have independent timers. Both go through a real checkpoint,
// and validate all restored counts and two payload bytes outside the timer.
func BenchmarkSparseSharedCheckpoint(b *testing.B) {
	const sharedPages = 1024
	const stride = 4
	const pageSize = uint64(hostarch.PageSize)
	for _, operation := range []string{"Save", "Load"} {
		b.Run(operation, func(b *testing.B) {
			b.StopTimer()
			newFile := func() *MemoryFile {
				file := openSaveTestFile(b, "")
				f, err := NewMemoryFile(file, MemoryFileOpts{
					DelayedEviction:         DelayedEvictionDisabled,
					DisableMemoryAccounting: true,
				})
				if err != nil {
					file.Close()
					b.Fatal(err)
				}
				return f
			}
			source := newFile()
			defer source.Destroy()
			backing, err := source.Allocate(sharedPages*stride*pageSize, AllocOpts{Mode: AllocateUncommitted})
			if err != nil {
				b.Fatal(err)
			}
			defer source.DecRef(backing)
			shared := make([]memmap.FileRange, sharedPages)
			for i := range shared {
				start := backing.Start + uint64(i)*stride*pageSize
				shared[i] = memmap.FileRange{Start: start, End: start + pageSize}
				source.IncRef(shared[i], 0)
			}
			defer func() {
				for _, fr := range shared {
					source.DecRef(fr)
				}
			}()
			writeMappedPage(source, backing, 0, 0x5a)
			writeMappedPage(source, backing, (sharedPages-1)*stride, 0xa5)
			save := func() []byte {
				var buf bytes.Buffer
				if err := source.SaveTo(context.Background(), &buf, &SaveOpts{}); err != nil {
					b.Fatal(err)
				}
				return buf.Bytes()
			}
			image := save()
			b.ReportAllocs()
			for range b.N {
				if operation == "Save" {
					b.StartTimer()
					image = save()
					b.StopTimer()
				}
				restored := newFile()
				if operation == "Load" {
					b.StartTimer()
				}
				if err := restored.LoadFrom(context.Background(), bytes.NewReader(image), &LoadOpts{}); err != nil {
					b.Fatal(err)
				}
				if operation == "Load" {
					b.StopTimer()
				}
				for _, fr := range shared {
					gap := memmap.FileRange{Start: fr.End, End: fr.Start + stride*pageSize}
					if restored.HasUniqueRef(fr) || !restored.HasUniqueRef(gap) {
						b.Fatalf("restored sharing differs for shared page %v or following private pages %v", fr, gap)
					}
				}
				for i, want := range []byte{0x5a, 0xa5} {
					var got [1]byte
					off := int64(shared[i*(sharedPages-1)].Start + 17)
					if n, err := unix.Pread(restored.FD(), got[:], off); err != nil || n != 1 || got[0] != want {
						b.Fatalf("read offset %d: got %x, n=%d, err=%v; want %x", off, got, n, err, want)
					}
				}
				for _, fr := range shared {
					restored.DecRef(fr)
				}
				if !restored.HasUniqueRef(backing) {
					b.Fatal("a page retained more than two references after restore")
				}
				restored.DecRef(backing)
				restored.Destroy()
			}
			b.ReportMetric(float64(len(image)), "checkpoint-B/op")
		})
	}
}
