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
	"context"
	"reflect"
	"testing"
	"unsafe"
)

// mapWord has a direct interface representation: its one pointer is the
// interface data word. The field is reconstructed by its typed load hook.
type mapWord struct {
	p *int
}

type mapWordObservation struct{ token, calls int }
type mapWordKey struct{}

func init() { RegisterStruct((*mapWord)(nil)) }

func (*mapWord) StateTypeName() string { return "state.testMapWord" }
func (*mapWord) StateFields() []string { return nil }
func (*mapWord) StateSave(Sink)        {}
func (v *mapWord) StateLoad(_ context.Context, s Source) {
	// At patch 16 the public AfterLoadTo adapter is not wired to the
	// graph. Register its typed callback using the existing graph method.
	ds := s.graph.ds
	ds.addCallback(ds.cur, decodeCallback{afterLoader: v, waiter: -1})
}
func (v *mapWord) StateAfterLoad(ctx context.Context) {
	o := ctx.Value(mapWordKey{}).(*mapWordObservation)
	o.calls++
	v.p = &o.token
}

// TestGraphMapPublishesDirectInterface drives the patch-16 encoder, value
// decoder and callback queue. This intermediate patch has no graph Load
// entry point; the fixture supplies the outer table/queue dispatch only.
func TestGraphMapPublishesDirectInterface(t *testing.T) {
	wt := typeInfoOf(reflect.TypeFor[mapWord]())
	it := typeInfoOf(reflect.TypeFor[any]())
	mt := typeInfoOf(reflect.TypeFor[map[uint8]any]())
	if !wt.direct || wt.kind != reflect.Struct {
		t.Fatal("the dynamic type must store its pointer directly in the interface")
	}
	for _, name := range []string{"interface_slot", "map_value", "map_pointer"} {
		t.Run(name, func(t *testing.T) {
			o := &mapWordObservation{token: 73}
			ctx := context.WithValue(context.Background(), mapWordKey{}, o)
			src := mapWord{p: &o.token}
			var v any = src
			if name == "map_pointer" {
				v = &src
			}
			es := newGraphEncodeState(ctx, it)
			defer es.release()
			if name == "interface_slot" {
				es.encodeValue(it, unsafe.Pointer(&v))
			} else {
				m := map[uint8]any{7: v}
				if n := es.encodeMap(mt, reflect.ValueOf(m).UnsafePointer()); n != 1 {
					t.Fatalf("encoded %d map entries, want 1", n)
				}
			}
			ds := newGraphDecodeState(ctx)
			defer ds.release()
			ds.types = append(ds.types, es.types.types...)
			ds.in = decBuf{b: es.out.b}
			ds.ods = append(ds.ods, graphObjectDecodeState{cbHead: -1, cbTail: -1, waitHead: -1, waitTail: -1})
			var pointee mapWord
			if name == "map_pointer" {
				// The encoded pointer identifies exactly one target. Allocate
				// its decoded storage, as the outer object reader would.
				if es.targets.n != 1 {
					t.Fatalf("encoded %d pointer targets, want 1", es.targets.n)
				}
				p := unsafe.Pointer(&pointee)
				ds.targets = append(ds.targets, p)
				ds.targetObjs = append(ds.targetObjs, 0)
				ds.objs = append(ds.objs, decObject{base: p, ti: wt, size: wt.size, kind: objKindValue})
				ds.decodeValue(wt, p)
			}
			var slot any
			m := make(map[uint8]any)
			if name == "interface_slot" {
				ds.decodeValue(it, unsafe.Pointer(&slot))
			} else {
				ds.decodeMap(mt, reflect.ValueOf(m).UnsafePointer(), 1)
			}
			if ds.in.pos != len(ds.in.b) {
				t.Fatalf("used %d of %d encoded bytes", ds.in.pos, len(ds.in.b))
			}
			// All three inputs have one object owning a typed callback.
			// Drain its original FIFO, including the deferred data-word copy.
			for i := ds.ods[0].cbHead; i >= 0; i = ds.cbs[i].next {
				ds.runCallback(&ds.cbs[i])
			}
			if o.calls != 1 {
				t.Fatalf("typed hook calls = %d, want 1", o.calls)
			}
			if name != "interface_slot" {
				var ok bool
				if slot, ok = m[7]; !ok || len(m) != 1 {
					t.Fatalf("restored map keys = %v, want only 7", m)
				}
			}
			var p *int
			if name == "map_pointer" {
				ref := slot.(*mapWord)
				if ref != &pointee {
					t.Fatal("map's pointer lost the decoded pointee")
				}
				p = ref.p
			} else {
				p = slot.(mapWord).p
			}
			t.Logf("wire=%x hook_calls=%d points_to_token=%v", es.out.b, o.calls, p == &o.token)
			if p != &o.token {
				t.Errorf("published pointer after the typed hook = %v, want the restored token 73", p)
			}
		})
	}
}
