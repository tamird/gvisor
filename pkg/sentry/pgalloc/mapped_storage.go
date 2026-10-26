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
	"context"
	"fmt"

	"gvisor.dev/gvisor/pkg/errors/linuxerr"
	"gvisor.dev/gvisor/pkg/hostarch"
	"gvisor.dev/gvisor/pkg/sentry/memmap"
	"gvisor.dev/gvisor/pkg/sentry/usage"
)

// MappedStorage owns a reference to each page in a contiguous mapping of the
// main MemoryFile. Its backing remains committed until Release, including
// across checkpoints. Sharing is the responsibility of its owner; MappedStorage
// does not have a separate reference count.
//
// +stateify savable
type MappedStorage struct {
	fr     memmap.FileRange
	length uint64
	mf     *MemoryFile `state:"nosave"`
	data   []byte      `state:"nosave"`
}

// AllocateMapped allocates length bytes, split into independently owned mappings
// of at most maxChunkSize bytes. The caller owns every returned MappedStorage
// and must either release it or transfer that ownership. On error no ownership
// is returned. Page rounding is not exposed by Bytes.
//
// Preconditions: length > 0; maxChunkSize is nonzero and page-aligned; f is the
// main MemoryFile supplied by MemoryFileFromContext during restore.
func (f *MemoryFile) AllocateMapped(length, maxChunkSize uint64, kind usage.MemoryKind, memCgID uint32) ([]*MappedStorage, error) {
	if length == 0 || maxChunkSize == 0 || hostarch.PageRoundDown(maxChunkSize) != maxChunkSize {
		panic("invalid mapped storage size")
	}
	allocLength, ok := hostarch.PageRoundUp(length)
	if !ok {
		return nil, linuxerr.ENOMEM
	}
	fr, err := f.Allocate(allocLength, AllocOpts{
		Kind:             kind,
		MemCgID:          memCgID,
		Mode:             AllocateAndCommit,
		RetainCommitment: true,
	})
	if err != nil {
		return nil, err
	}
	blocks, err := f.MapInternal(fr, hostarch.ReadWrite)
	if err != nil {
		f.DecRef(fr)
		return nil, err
	}
	var storage []*MappedStorage
	offset := fr.Start
	for !blocks.IsEmpty() {
		data := blocks.Head().ToSlice()
		for len(data) != 0 {
			// Each block boundary is page-aligned. Partition the allocation's
			// initial references; retaining an additional reference to fr would
			// leak pages after the last storage owner releases them.
			n := min(uint64(len(data)), maxChunkSize)
			visible := min(n, length)
			storage = append(storage, &MappedStorage{
				fr:     memmap.FileRange{Start: offset, End: offset + n},
				length: visible,
				mf:     f,
				data:   data[:visible:visible],
			})
			offset += n
			length -= visible
			data = data[n:]
		}
		blocks = blocks.Tail()
	}
	return storage, nil
}

// Bytes returns the same writable slice until Release. After state.Load it may
// only be called after MappedStorageRestore.Restore has succeeded. It never
// allocates, loads pages, or performs a fallible mapping operation.
func (s *MappedStorage) Bytes() []byte {
	if s.data == nil {
		panic("mapped storage is not ready")
	}
	return s.data
}

// Release releases the owned page references. It must be called exactly once,
// before the MemoryFile is destroyed. Restored MemoryFile range metadata and
// references must already be loaded. It does not require Bytes to be ready, so
// filesystem extraction and failed page loads can free cold packet storage.
// Partial object graphs or failed metadata loads do not support per-object
// cleanup; the failed kernel is discarded without calling Release.
func (s *MappedStorage) Release() {
	s.mf.DecRef(s.fr)
	s.mf = nil
	s.data = nil
}

// afterLoad is invoked by stateify.
func (s *MappedStorage) afterLoad(ctx context.Context) {
	s.mf = MemoryFileFromContext(ctx)
	if s.mf == nil {
		panic("mapped storage restore requires the main MemoryFile")
	}
	if v := ctx.Value(CtxMappedStorageRestore); v != nil {
		r := v.(*MappedStorageRestore)
		r.storage = append(r.storage, s)
	}
}

// MappedStorageRestore collects borrowed storage owners during one state.Load.
// The graph owns their page references; this collection must not release them.
// Registration and Restore are sequential, as are state.Load callbacks.
type MappedStorageRestore struct {
	storage []*MappedStorage
}

// Clear drops borrowed pointers without accessing or releasing storage. Call it
// on every return path from a restore, including failed state or MemoryFile loads.
func (r *MappedStorageRestore) Clear() {
	r.storage = nil
}

// Restore makes all collected storage usable after MemoryFile metadata and
// mappings have loaded. MapInternal waits for asynchronous page data and returns
// any loading error. No buffer consumer may run until this method succeeds.
// On error, graph teardown must release even the storage not yet mapped.
func (r *MappedStorageRestore) Restore() error {
	defer r.Clear()
	for _, s := range r.storage {
		blocks, err := s.mf.MapInternal(s.fr, hostarch.ReadWrite)
		if err != nil {
			return fmt.Errorf("restore mapped storage %v: %w", s.fr, err)
		}
		// AllocateMapped splits at MemoryFile chunk boundaries, which remain
		// unchanged across save/restore.
		if blocks.IsEmpty() || !blocks.Tail().IsEmpty() {
			return fmt.Errorf("restored storage %v is not contiguous", s.fr)
		}
		s.data = blocks.Head().ToSlice()[:s.length:s.length]
	}
	return nil
}
