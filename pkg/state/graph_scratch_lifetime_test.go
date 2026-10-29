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
	"reflect"
	"runtime"
	"runtime/debug"
	"testing"
	"unsafe"
	"weak"
)

type scratchPayload = [4096]byte

type scratchValue struct {
	value   uint64
	alias   *uint64
	payload *scratchPayload
	self    bool
}

func (*scratchValue) StateTypeName() string { return "state.scratchValue" }
func (*scratchValue) StateFields() []string { return []string{"value", "alias", "payload"} }

type scratchTraceKey struct{}
type scratchTrace struct {
	es    *graphEncodeState
	addrs []uintptr
}

func (v *scratchValue) StateSave(s Sink) {
	tr := s.Context().Value(scratchTraceKey{}).(*scratchTrace)
	tr.es = s.graph.es
	tr.addrs = append(tr.addrs, uintptr(unsafe.Pointer(v)))
	if v.self {
		v.alias = &v.value
	}
	s.Save(0, &v.value)
	s.Save(1, &v.alias)
	s.Save(2, &v.payload)
}

func (v *scratchValue) StateLoad(_ context.Context, s Source) {
	s.Load(0, &v.value)
	s.Load(1, &v.alias)
	s.Load(2, &v.payload)
}

func init() { RegisterStruct((*scratchValue)(nil)) }

// saveScratch returns no strong handle to the original graph or copied value.
// It keeps the exact graph encoder the public Save returned to its pool.
func saveScratch(t *testing.T, prime, alias bool) (*graphEncodeState, weak.Pointer[scratchPayload], bool) {
	t.Helper()
	tr := &scratchTrace{}
	ctx := context.WithValue(t.Context(), scratchTraceKey{}, tr)
	payload := new(scratchPayload)
	payload[123] = 67
	data := []any{}
	if prime {
		data = append(data, scratchValue{value: 17})
	}
	data = append(data, scratchValue{value: 29, payload: payload, self: alias})
	victim := weak.Make(payload)
	var buf bytes.Buffer
	if _, err := Save(ctx, &buf, &data); err != nil {
		t.Fatalf("Save: %v", err)
	}
	if got, want := len(tr.addrs), len(data); got != want {
		t.Fatalf("StateSave calls = %d, want %d", got, want)
	}
	if tr.es == nil || tr.es.ctx != nil || tr.es.objs.n != 0 || tr.es.targets.n != 0 {
		t.Fatal("Save did not release the observed graph encoder")
	}
	var restored []any
	if _, err := Load(t.Context(), &buf, &restored); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got, want := len(restored), len(data); got != want {
		t.Fatalf("restored length = %d, want %d", got, want)
	}
	v := restored[len(restored)-1].(scratchValue)
	if v.value != 29 || v.payload == nil || v.payload[123] != 67 || (v.alias != nil) != alias || (v.alias != nil && *v.alias != v.value) {
		t.Fatalf("invalid round trip: value=%d alias=%p payload present=%t", v.value, v.alias, v.payload != nil)
	}
	reused := len(tr.addrs) == 2 && tr.addrs[0] == tr.addrs[1]
	if got, want := reused, prime; got != want {
		t.Fatalf("reused scratch = %t, want %t", got, want)
	}
	return tr.es, victim, reused
}

// scratchFree reports what owns the original allocation after release.
// Keep any temporary reflection values out of the calling GC frame.
func scratchFree(t *testing.T, es *graphEncodeState, alias bool, victim weak.Pointer[scratchPayload]) (int, int, bool) {
	t.Helper()
	free := es.copies[typeInfoOf(reflect.TypeFor[scratchValue]())]
	if !alias {
		if len(free) != 1 || !free[0].IsZero() {
			t.Fatal("ordinary copy was not returned zeroed to the free list")
		}
	} else if len(free) != 0 {
		t.Fatalf("aliased copies on free list = %d, want 0", len(free))
	}
	for _, v := range free[len(free):cap(free)] {
		if !v.IsValid() {
			continue
		}
		x := v.Addr().Interface().(*scratchValue)
		if x.alias != &x.value || x.payload != victim.Value() {
			t.Fatal("inactive slot does not contain the saved self-alias and original payload")
		}
		return len(free), cap(free), true
	}
	return len(free), cap(free), false
}

// pooledScratch returns only weak references; in particular it does not keep
// the released encoder alive to obtain a positive pool-retention observation.
func pooledScratch(t *testing.T) (weak.Pointer[graphEncodeState], weak.Pointer[scratchPayload]) {
	es, victim, _ := saveScratch(t, true, true)
	return weak.Make(es), victim
}

//go:noinline
func weakAlive[T any](w weak.Pointer[T]) bool { return w.Value() != nil }

func TestGraphScratchLifetime(t *testing.T) {
	old := GraphEngineEnabled()
	EnableGraphEngine(true)
	t.Cleanup(func() { EnableGraphEngine(old) })
	p := runtime.GOMAXPROCS(1)
	t.Cleanup(func() { runtime.GOMAXPROCS(p) })
	gc := debug.SetGCPercent(-1)
	t.Cleanup(func() { debug.SetGCPercent(gc) })

	for _, tc := range []struct {
		name         string
		prime, alias bool
	}{
		{"reuse_plain", true, false},
		{"fresh_alias", false, true},
		{"reuse_alias", true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// Start with no encoder remaining from an earlier subtest.
			runtime.GC()
			runtime.GC()
			es, victim, reused := saveScratch(t, tc.prime, tc.alias)
			n, capacity, stale := scratchFree(t, es, tc.alias, victim)
			runtime.GC()
			alive := weakAlive(victim)
			// Clear only the hidden stack slots on this exact encoder. If
			// anything else retained the original graph it would still be live.
			free := es.copies[typeInfoOf(reflect.TypeFor[scratchValue]())]
			clear(free[len(free):cap(free)])
			runtime.GC()
			after := weakAlive(victim)
			runtime.KeepAlive(es)
			t.Logf("SCRATCH roundtrip=1 reused=%t self=%t len=%d cap=%d stale=%t alive=%t after_clear=%t", reused, tc.alias, n, capacity, stale, alive, after)
			if stale || alive || after {
				t.Errorf("post-release inactive slot present=%t, original live=%t (after clearing: %t); want all false", stale, alive, after)
			}
		})
	}
	t.Run("pool_only", func(t *testing.T) {
		runtime.GC()
		runtime.GC()
		es, victim := pooledScratch(t)
		runtime.GC()
		one, payloadOne := weakAlive(es), weakAlive(victim)
		runtime.GC()
		two, payloadTwo := weakAlive(es), weakAlive(victim)
		t.Logf("POOL roundtrip=1 cache_first=%t payload_first=%t cache_second=%t payload_second=%t", one, payloadOne, two, payloadTwo)
		if payloadOne || payloadTwo {
			t.Errorf("pooled original live after one/two GCs = %t/%t; want false/false", payloadOne, payloadTwo)
		}
	})
}
