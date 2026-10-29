// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package state

import (
	"bytes"
	"context"
	"math/rand"
	"reflect"
	"runtime"
	"testing"
	"unsafe"
)

func TestAddrMap(t *testing.T) {
	r := rand.New(rand.NewSource(1))
	for _, initial := range []int{0, 1, 1000} {
		var m addrMap
		if initial > 0 {
			m.init(initial)
		}
		want := make(map[uintptr]int32)
		for round := 0; round < 3; round++ {
			for i := 0; i < 5000; i++ {
				// Use clustered keys, like addresses, and zero.
				k := uintptr(r.Intn(20000)) * 8
				if i%3 == 0 {
					k = uintptr(r.Uint64())
				}
				if k == ^uintptr(0) {
					continue
				}
				v := int32(r.Intn(1 << 30))
				m.set(k, v)
				want[k] = v
			}
			for k, v := range want {
				if got, ok := m.get(k); !ok || got != v {
					t.Fatalf("get(%#x) = %d, %t, want %d, true", k, got, ok, v)
				}
			}
			for i := 0; i < 1000; i++ {
				k := uintptr(r.Intn(40000))*8 + 4
				got, gotOK := m.get(k)
				wantV, wantOK := want[k]
				if got != wantV || gotOK != wantOK {
					t.Fatalf("get(%#x) = (%d, %t), want (%d, %t)", k, got, gotOK, wantV, wantOK)
				}
			}
			if m.n != len(want) {
				t.Fatalf("got %d entries, want %d", m.n, len(want))
			}
			m.reset()
			clear(want)
			if _, ok := m.get(8); ok {
				t.Fatalf("get after reset found a key")
			}
			if _, ok := m.get(^uintptr(0)); ok {
				t.Fatalf("get(max uintptr) unexpectedly succeeded")
			}
		}
	}

	var m addrMap
	m.set(0, 7)
	func() {
		defer func() {
			if recover() == nil {
				t.Fatalf("set(max uintptr) did not fail")
			}
		}()
		m.set(^uintptr(0), 1)
	}()
	if got, ok := m.get(0); !ok || got != 7 {
		t.Fatalf("get(0) = %d, %t, want 7, true", got, ok)
	}
}

// TestGraphAddressIndexStudy measures the stock encoder's address-index shape.
// It records metadata capacity, not whole-process memory or elapsed time.
func TestGraphAddressIndexStudy(t *testing.T) {
	ctx := context.Background()
	ti := typeInfoOf(reflect.TypeFor[[]byte]())
	var wireSizes []int
	for _, tc := range []struct {
		name     string
		capacity int
	}{{"one_byte", 1}, {"64_KiB", 64 << 10}, {"16_MiB", 16 << 20}} {
		t.Run(tc.name, func(t *testing.T) {
			payload := make([]byte, 1, tc.capacity)
			payload[0] = 0xa9
			s := newGraphEncodeState(ctx, ti)
			// Keep this diagnostic state out of the shared pool.
			s.save(unsafe.Pointer(&payload), ti, &s.final)
			var encoded bytes.Buffer
			if _, err := saveGraph(ctx, &encoded, &payload); err != nil {
				t.Fatal(err)
			}
			if len(s.final.b) == 0 || !bytes.HasSuffix(encoded.Bytes(), s.final.b) {
				t.Fatal("instrumented state disagrees with the unchanged graph writer")
			}
			tid := s.lookup(uintptr(unsafe.Pointer(&payload[0])), ti.elem)
			if tid < 0 {
				t.Fatal("slice target missing")
			}
			obj, off := s.root(s.target(tid).obj)
			o := s.obj(obj)
			if !o.isArray || off != 0 || o.n != uintptr(tc.capacity) {
				t.Fatalf("slice target is not the original capacity: array=%t offset=%d capacity=%d", o.isArray, off, o.n)
			}
			var live uintptr
			for _, ch := range s.chunkList(o) {
				live += ch.end - ch.start
			}
			if live != 1 {
				t.Fatalf("encoded %d live elements, want one", live)
			}
			wireSizes = append(wireSizes, encoded.Len())
			t.Logf("INDEX slice_cap=%d live=%d wire=%d keys=%d slots=%d entries=%d entry_cap=%d slot_bytes=%d entry_bytes=%d",
				tc.capacity, live, encoded.Len(), s.blocks.n, len(s.blocks.slots), len(s.blockEntries), cap(s.blockEntries),
				uintptr(len(s.blocks.slots))*unsafe.Sizeof(addrSlot{}), uintptr(cap(s.blockEntries))*unsafe.Sizeof(blockEntry{}))
			runtime.KeepAlive(payload)
		})
	}
	if len(wireSizes) == 3 && (wireSizes[2] < wireSizes[0] || wireSizes[2]-wireSizes[0] >= 32) {
		t.Fatalf("large capacity unexpectedly changed stream size: %v", wireSizes)
	}
	t.Run("shared_block", func(t *testing.T) {
		const block = 1 << blockShift
		data := make([]byte, 34*block)
		base := uintptr(unsafe.Pointer(&data[0]))
		start := int((-base) & (block - 1))
		var s graphEncodeState
		elem := ti.elem
		add := func(offset int) int32 {
			p := unsafe.Pointer(&data[start+offset])
			obj := s.newObject(p, uintptr(p)+1, elem, 1, false)
			s.index(obj)
			return obj
		}
		for i := range 32 {
			add(i * block)
		}
		keys, slots := s.blocks.n, len(s.blocks.slots)
		last := add(31*block + 1)
		target := base + uintptr(start+31*block+1)
		got := s.query(target, target+1)
		if len(got) != 1 || got[0] != last || s.blocks.n != keys {
			t.Fatalf("lookup %v, want [%d]; key count %d→%d", got, last, keys, s.blocks.n)
		}
		t.Logf("INDEX shared keys=%d→%d slots=%d→%d entries=%d", keys, s.blocks.n, slots, len(s.blocks.slots), len(s.blockEntries))
		runtime.KeepAlive(data)
	})
}
