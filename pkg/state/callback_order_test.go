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
	"reflect"
	"testing"
)

func TestCompletionOrderAndLateWait(t *testing.T) {
	// Object 1 unblocks objects 2 and 3. Both the duplicate waits and a
	// wait registered while object 1's FIFO is draining must be retained.
	ds := decodeState{objs: make([]decObject, 4), ods: make([]objectDecodeState, 4)}
	for i := range ds.ods {
		ds.ods[i].cbHead, ds.ods[i].cbTail = -1, -1
	}
	src := Source{internal: objectDecoder{ds: &ds}}
	var order []string
	hook := func(obj int32, f func()) { ds.cur = obj; src.AfterLoad(f) }
	ds.wait(2, 1, callback{})
	hook(1, func() {
		order = append(order, "between duplicate waits")
		if got := ds.ods[2].blockedBy; got != 1 {
			t.Errorf("between waits: blockedBy=%d, want 1", got)
		}
		ds.wait(3, 1, callback{})
		src.AfterLoad(func() { order = append(order, "appended hook") })
	})
	ds.wait(2, 1, callback{})
	ds.wait(3, 1, callback{})
	hook(1, func() {
		order = append(order, "original tail")
		if got := ds.ods[3].blockedBy; got != 1 {
			t.Errorf("before appended wait: blockedBy=%d, want 1", got)
		}
	})
	hook(2, func() { order = append(order, "first dependent") })
	hook(3, func() { order = append(order, "second dependent") })
	ds.cur = 1
	ds.runCallbacks()
	want := []string{"between duplicate waits", "original tail", "appended hook", "first dependent", "second dependent"}
	if !reflect.DeepEqual(order, want) {
		t.Fatalf("order=%v, want %v", order, want)
	}
	for i, s := range ds.ods {
		if !s.complete || s.blockedBy != 0 {
			t.Errorf("object %d: complete=%t blockedBy=%d", i, s.complete, s.blockedBy)
		}
	}
}

func TestPooledCallbacksReleaseReferences(t *testing.T) {
	// Keep the backing store reachable independently of sync.Pool, which
	// may drop an entry at any time. Verify even the unused tail is clear.
	ds := decodeState{cbs: make([]callback, 16)}
	payload := new([128]byte)
	for i := range ds.cbs {
		ds.cbs[i] = callback{fn: func() { payload[0]++ }, valueFn: func(any) { payload[1]++ }, value: payload}
	}
	backing := ds.cbs
	ds.cbs = ds.cbs[:8]
	ds.release()
	for i, cb := range backing {
		if cb.user() || cb.value != nil {
			t.Fatalf("pooled slot %d retained callback data", i)
		}
	}
}
