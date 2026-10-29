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

package tests

import (
	"bytes"
	"context"
	"fmt"
	"reflect"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
)

// saveBytes saves the graph rooted at root.
func saveBytes(t testing.TB, root any) []byte {
	t.Helper()
	var buf bytes.Buffer
	if _, err := state.Save(context.Background(), &buf, root); err != nil {
		t.Fatalf("Save failed: %v", err)
	}
	return buf.Bytes()
}

// loadBytes loads a graph saved by saveBytes into a new T.
func loadBytes[T any](t testing.TB, data []byte) *T {
	t.Helper()
	root := new(T)
	if _, err := state.Load(context.Background(), bytes.NewReader(data), root); err != nil {
		t.Fatalf("Load failed: %v", err)
	}
	return root
}

// reachable calls fn for each addressable value of type T that is reachable
// from root through saved fields, once per distinct address.
func reachable[T any](root any, fn func(*T)) {
	target := reflect.TypeFor[T]()
	seen := make(map[addrKey]bool)
	seenMaps := make(map[uintptr]bool)
	var walk func(v reflect.Value)
	walk = func(v reflect.Value) {
		if v.CanAddr() && v.Type().Size() != 0 {
			k := addrKey{v.UnsafeAddr(), v.Type()}
			if seen[k] {
				return
			}
			seen[k] = true
			if v.Type() == target {
				fn((*T)(v.Addr().UnsafePointer()))
			}
		}
		switch v.Kind() {
		case reflect.Pointer, reflect.Interface:
			if !v.IsNil() {
				walk(v.Elem())
			}
		case reflect.Array:
			for i := 0; i < v.Len(); i++ {
				walk(v.Index(i))
			}
		case reflect.Slice:
			for i := 0; i < v.Len(); i++ {
				walk(v.Index(i))
			}
		case reflect.Map:
			if v.IsNil() || seenMaps[v.Pointer()] {
				return
			}
			seenMaps[v.Pointer()] = true
			for it := v.MapRange(); it.Next(); {
				walk(it.Key())
				walk(it.Value())
			}
		case reflect.Struct:
			typ := v.Type()
			if typ.PkgPath() == "sync/atomic" {
				if p := v.FieldByName("v"); p.IsValid() && p.Kind() == reflect.UnsafePointer && !unRO(p).IsNil() {
					walk(reflect.NewAt(typ.Field(0).Type.Elem().Elem(), unRO(p).UnsafePointer()).Elem())
				}
				return
			}
			for i := 0; i < typ.NumField(); i++ {
				if savedField(typ.Field(i)) {
					walk(v.Field(i))
				}
			}
		}
	}
	walk(reflect.ValueOf(root))
}

// sliceWindowsSupported indicates whether the state package supports saving
// slices that share a backing array at different offsets, or with elements of
// one slice beyond the length of another. The legacy encoder clears elements
// beyond a slice's length in the saved graph, and can't express a slice that
// starts within another slice's array.
const sliceWindowsSupported = true

func skipUnlessSliceWindows(t *testing.T) {
	t.Helper()
	if !sliceWindowsSupported {
		t.Skip("slice windows are not supported")
	}
}

var graphConfigs = []graphOptions{
	{nodes: 1},
	{nodes: 2, all: true},
	{nodes: 10},
	{nodes: 10, all: true, windows: true},
	{nodes: 50, windows: true},
	{nodes: 50, all: true},
	{nodes: 200, all: true, windows: true},
}

// TestRandomGraphs saves and loads random object graphs, and checks that the
// loaded graphs are isomorphic to the originals.
func TestRandomGraphs(t *testing.T) {
	seeds := int64(25)
	if testing.Short() {
		seeds = 5
	}
	for _, opts := range graphConfigs {
		for seed := int64(0); seed < seeds; seed++ {
			t.Run(fmt.Sprintf("nodes=%d,all=%v,windows=%v/seed=%d", opts.nodes, opts.all, opts.windows, seed), func(t *testing.T) {
				if opts.windows {
					skipUnlessSliceWindows(t)
				}
				original := randomGraph(seed, opts)
				saved := randomGraph(seed, opts)
				data := saveBytes(t, saved)

				// Save must not modify the saved graph.
				if err := isomorphic(original, saved); err != nil {
					t.Fatalf("Save modified the graph: %v", err)
				}
				// beforeSave must be called exactly once per object.
				reachable(saved, func(n *gNode) {
					if n.saveCalls != 1 {
						t.Errorf("node %d: beforeSave called %d times, want 1", n.id, n.saveCalls)
					}
				})

				loaded := loadBytes[gRoot](t, data)
				checkIsomorphic(t, original, loaded)
				reachable(loaded, func(n *gNode) {
					if !n.loaded {
						t.Errorf("node %d: afterLoad not called", n.id)
					}
				})

				// Saving the loaded graph must produce an equivalent graph.
				reloaded := loadBytes[gRoot](t, saveBytes(t, loaded))
				checkIsomorphic(t, original, reloaded)
			})
		}
	}
}

// TestStringSlabStudy compares the same wire stream under both policies.
func TestStringSlabStudy(t *testing.T) {
	one := "xy"
	oneWire := saveBytes(t, &one)
	graph := randomGraph(1, graphOptions{nodes: benchGraphNodes, all: true})
	graphWire := saveBytes(t, graph)
	for _, mode := range []string{"stock", "bounded"} {
		t.Run(mode, func(t *testing.T) {
			t.Setenv("GVISOR_STUDY_STRING_SLAB", mode)
			t.Run("OneString", func(t *testing.T) {
				if got := *loadBytes[string](t, oneWire); got != one {
					t.Fatalf("got %q, want %q", got, one)
				}
			})
			t.Run("BenchmarkGraph", func(t *testing.T) {
				checkIsomorphic(t, graph, loadBytes[gRoot](t, graphWire))
			})
		})
	}
}

// TestSharedSliceWindows checks that slices sharing a backing array still
// share it after loading, and that saving one of them does not modify the
// elements of the others.
func TestSharedSliceWindows(t *testing.T) {
	newBacking := func() []gInner {
		b := make([]gInner, 4, 6)
		for i := range b {
			b[i] = gInner{x: int64(100 + i), s: fmt.Sprintf("e%d", i)}
		}
		return b
	}
	for _, test := range []struct {
		name   string
		window func(b []gInner) []gInner
	}{
		{"prefix", func(b []gInner) []gInner { return b[:1] }},
		{"middle", func(b []gInner) []gInner { return b[1:2] }},
		{"empty", func(b []gInner) []gInner { return b[2:2] }},
		{"suffix", func(b []gInner) []gInner { return b[3:] }},
		{"beyond_len", func(b []gInner) []gInner { return b[2:6] }},
		{"full", func(b []gInner) []gInner { return b[:] }},
	} {
		for _, windowFirst := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/window_first=%v", test.name, windowFirst), func(t *testing.T) {
				if test.name != "full" {
					skipUnlessSliceWindows(t)
				}
				build := func() *gRoot {
					full, part := &gNode{id: 1}, &gNode{id: 2}
					full.vals = newBacking()
					part.window = test.window(full.vals)
					if windowFirst {
						return &gRoot{entries: []*gNode{part, full}}
					}
					return &gRoot{entries: []*gNode{full, part}}
				}
				original, saved := build(), build()
				data := saveBytes(t, saved)
				if err := isomorphic(original, saved); err != nil {
					t.Fatalf("Save modified the graph: %v", err)
				}
				checkIsomorphic(t, original, loadBytes[gRoot](t, data))
			})
		}
	}
}

// TestInteriorPointersIntoSlices checks pointers to the elements of slices and
// to fields of those elements, discovered before or after the slice.
func TestInteriorPointersIntoSlices(t *testing.T) {
	for _, ptrFirst := range []bool{false, true} {
		t.Run(fmt.Sprintf("ptr_first=%v", ptrFirst), func(t *testing.T) {
			build := func() *gRoot {
				owner := &gNode{id: 1, vals: []gInner{{x: 1}, {x: 2}, {x: 3}}, blob: []byte("hello")}
				ptrs := &gNode{id: 2}
				ptrs.innerPtr = &owner.vals[1]
				ptrs.intPtr = &owner.vals[2].x
				ptrs.bytePtr = &owner.blob[4]
				ptrs.iface = &owner.vals[0]
				if ptrFirst {
					return &gRoot{entries: []*gNode{ptrs, owner}}
				}
				return &gRoot{entries: []*gNode{owner, ptrs}}
			}
			original := build()
			loaded := loadBytes[gRoot](t, saveBytes(t, build()))
			checkIsomorphic(t, original, loaded)
		})
	}
}

// TestMapIdentity checks that a map referenced in several ways is loaded as a
// single map.
func TestMapIdentity(t *testing.T) {
	build := func() *gRoot {
		a, b := &gNode{id: 1}, &gNode{id: 2}
		a.shared = map[string]int64{"x": 1, "y": 2}
		b.shared = a.shared
		b.sharedPtr = &a.shared
		b.iface = a.shared
		return &gRoot{entries: []*gNode{b, a}, any: a.shared}
	}
	original := build()
	loaded := loadBytes[gRoot](t, saveBytes(t, build()))
	checkIsomorphic(t, original, loaded)
	a, b := loaded.entries[1], loaded.entries[0]
	b.shared["z"] = 3
	if a.shared["z"] != 3 || loaded.any.(map[string]int64)["z"] != 3 || (*b.sharedPtr)["z"] != 3 {
		t.Errorf("maps are not shared after loading")
	}
	if b.sharedPtr != &a.shared {
		t.Errorf("sharedPtr does not point to a.shared")
	}
}

// TestNilAndEmpty checks that nil and empty slices and maps are distinct after
// loading.
func TestNilAndEmpty(t *testing.T) {
	build := func() *gRoot {
		nilNode := &gNode{id: 1}
		emptyNode := &gNode{id: 2, nodes: []*gNode{}, vals: make([]gInner, 0, 3), blob: []byte{}, byID: map[int64]*gNode{}, shared: map[string]int64{}, ifaces: []any{}}
		return &gRoot{entries: []*gNode{nilNode, emptyNode}}
	}
	original := build()
	loaded := loadBytes[gRoot](t, saveBytes(t, build()))
	checkIsomorphic(t, original, loaded)
}

// TestWaitOrdering checks that afterLoad waits for objects referred to by wait
// fields and arrays, along a long chain.
func TestWaitOrdering(t *testing.T) {
	for _, field := range []string{"wait", "array"} {
		t.Run(field, func(t *testing.T) {
			build := func() *gRoot {
				nodes := make([]*gNode, 100)
				for i := range nodes {
					nodes[i] = &gNode{id: int64(i)}
				}
				for i := 0; i+1 < len(nodes); i++ {
					if field == "wait" {
						nodes[i].waitFor = nodes[i+1]
					} else {
						nodes[i].later[1] = nodes[i+1]
					}
					// Also point backwards, without waiting.
					nodes[i+1].next = nodes[i]
				}
				return &gRoot{first: nodes[0]}
			}
			loaded := loadBytes[gRoot](t, saveBytes(t, build()))
			checkIsomorphic(t, build(), loaded)
		})
	}
}

// TestValueFields checks that fields saved as values of other types are
// loaded, including pointers saved by value.
func TestValueFields(t *testing.T) {
	build := func() *gRoot {
		a, b := &gNode{id: 1, saved: 42}, &gNode{id: 2, saved: -7}
		a.savedNode = b
		b.savedNode = b
		a.alias.Store(b)
		b.next = a
		return &gRoot{first: a}
	}
	loaded := loadBytes[gRoot](t, saveBytes(t, build()))
	checkIsomorphic(t, build(), loaded)
	if got := loaded.first.alias.Load().next; got != loaded.first {
		t.Errorf("alias cycle broken: got %p, want %p", got, loaded.first)
	}
}

// TestTruncatedStreams checks that loading fails for every truncation of a
// saved graph.
func TestTruncatedStreams(t *testing.T) {
	data := saveBytes(t, randomGraph(1, graphOptions{nodes: 20, all: true, windows: sliceWindowsSupported}))
	step := 1
	if testing.Short() {
		step = 7
	}
	for l := 0; l < len(data); l += step {
		root := new(gRoot)
		if _, err := state.Load(context.Background(), bytes.NewReader(data[:l]), root); err == nil {
			t.Fatalf("Load of %d/%d bytes succeeded", l, len(data))
		}
	}
}

// TestWrongRootType checks that loading into a root of another type fails.
func TestWrongRootType(t *testing.T) {
	data := saveBytes(t, randomGraph(1, graphOptions{nodes: 5}))
	var wrong gNode
	if _, err := state.Load(context.Background(), bytes.NewReader(data), &wrong); err == nil {
		t.Fatalf("Load into the wrong root type succeeded")
	}
}

// gIDs is an unregistered named slice type.
type gIDs []gID

// TestUnregisteredInterfaceValues checks that values of unregistered named
// types can't be saved in interfaces, since Load could not restore their
// types.
func TestUnregisteredInterfaceValues(t *testing.T) {
	for _, v := range []any{gID(1), gIDs{1, 2}, &gIDs{3}} {
		var buf bytes.Buffer
		if _, err := state.Save(context.Background(), &buf, &gRoot{any: v}); err == nil {
			t.Errorf("Save of %T in an interface succeeded", v)
		}
	}
}

// TestIsomorphismChecker checks that the isomorphism checker detects
// differences in aliasing.
func TestIsomorphismChecker(t *testing.T) {
	shared := &gNode{id: 3}
	aliased := &gRoot{entries: []*gNode{shared, shared}}
	distinct := &gRoot{entries: []*gNode{{id: 3}, {id: 3}}}
	if err := isomorphic(aliased, distinct); err == nil {
		t.Errorf("checker did not detect lost aliasing")
	}
	if err := isomorphic(distinct, aliased); err == nil {
		t.Errorf("checker did not detect added aliasing")
	}
	owner := &gNode{id: 1}
	interior := &gRoot{first: owner, any: &owner.inner}
	copied := &gRoot{first: &gNode{id: 1}, any: &gInner{}}
	if err := isomorphic(interior, copied); err == nil {
		t.Errorf("checker did not detect lost interior pointer")
	}
	backing := []gInner{{x: 1}, {x: 2}}
	windowed := &gRoot{first: &gNode{vals: backing, window: backing[1:]}}
	unwindowed := &gRoot{first: &gNode{vals: []gInner{{x: 1}, {x: 2}}, window: []gInner{{x: 2}}}}
	if err := isomorphic(windowed, unwindowed); err == nil {
		t.Errorf("checker did not detect lost slice window")
	}
	m := map[string]int64{"a": 1}
	sharedMap := &gRoot{entries: []*gNode{{shared: m}, {shared: m}}}
	copiedMap := &gRoot{entries: []*gNode{{shared: map[string]int64{"a": 1}}, {shared: map[string]int64{"a": 1}}}}
	if err := isomorphic(sharedMap, copiedMap); err == nil {
		t.Errorf("checker did not detect lost map aliasing")
	}
	if err := isomorphic(sharedMap, sharedMap); err != nil {
		t.Errorf("checker rejected identical graphs: %v", err)
	}
}

// TestScatteredArrayElements checks arrays of which disjoint elements are
// pointed to, in various orders, before and after slices of them.
func TestScatteredArrayElements(t *testing.T) {
	for _, order := range [][]int{
		{8, 2, 5, 0, 9},
		{0, 9, 5, 2, 8},
		{5, 4, 6, 3, 7},
		{9, 7, 5, 3, 1},
	} {
		for _, sliceFirst := range []bool{false, true} {
			t.Run(fmt.Sprintf("%v/slice_first=%v", order, sliceFirst), func(t *testing.T) {
				build := func() *gRoot {
					backing := make([]gInner, 10, 12)
					for i := range backing {
						backing[i] = gInner{x: int64(i + 1)}
					}
					var entries []*gNode
					for _, i := range order {
						entries = append(entries, &gNode{innerPtr: &backing[i], intPtr: &backing[i].x})
					}
					entries = append(entries, &gNode{window: backing[3:4]}, &gNode{window: backing[6:8:9]})
					owner := &gNode{vals: backing[:2]}
					if sliceFirst {
						entries = append([]*gNode{owner}, entries...)
					} else {
						entries = append(entries, owner)
					}
					return &gRoot{entries: entries}
				}
				original := build()
				loaded := loadBytes[gRoot](t, saveBytes(t, build()))
				checkIsomorphic(t, original, loaded)
			})
		}
	}
}

// TestRootZeroFields checks that the zero values of fields of the root do not
// overwrite their existing values, including byte arrays.
func TestRootZeroFields(t *testing.T) {
	data := saveBytes(t, &gBytesRoot{})
	want := gBytesRoot{b: [1]byte{7}, arr: [3]byte{1, 2, 3}, n: 5, s: "x"}
	got := want
	if _, err := state.Load(context.Background(), bytes.NewReader(data), &got); err != nil {
		t.Fatalf("Load failed: %v", err)
	}
	if got != want {
		t.Errorf("Load of zero fields changed the root: got %+v, want %+v", got, want)
	}
	data = saveBytes(t, &gBytesRoot{b: [1]byte{9}, n: 3})
	if _, err := state.Load(context.Background(), bytes.NewReader(data), &got); err != nil {
		t.Fatalf("Load failed: %v", err)
	}
	want.b, want.n = [1]byte{9}, 3
	if got != want {
		t.Errorf("Load of non-zero fields: got %+v, want %+v", got, want)
	}
}
