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
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"testing/iotest"

	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/tests/foreign"
)

// graphBuilder builds random object graphs.
type graphBuilder struct {
	seed  int
	r     *rand.Rand
	nodes []*graphNode
	// hasMaps is set if the graph has a map with more than one entry. Its
	// encoding depends on iteration order, as does the order in which
	// objects are found (and so object IDs and the order of types).
	hasMaps bool
}

func (b *graphBuilder) leaf() graphLeaf {
	r := b.r
	l := graphLeaf{}
	if r.Intn(4) == 0 {
		return l // Zero value.
	}
	l.A = int8(r.Intn(256) - 128)
	l.B = r.Uint32() >> uint(r.Intn(32))
	if r.Intn(2) == 0 {
		l.C = string(rune('a' + r.Intn(26)))
	}
	l.D = r.NormFloat64()
	l.E = r.Intn(2) == 0
	l.F = graphName(r.Intn(70000))
	l.G = complex(r.Float32(), -r.Float32())
	for i := range l.H {
		l.H[i] = byte(r.Intn(3) * 100)
	}
	l.I = uintptr(r.Uint64() >> uint(r.Intn(64)))
	return l
}

// ownsLeaf returns true if l is a leaf of the graph outside maps.
func (b *graphBuilder) ownsLeaf(l *graphLeaf) bool {
	for _, nd := range b.nodes {
		if l == &nd.Leaf || l == nd.LeafPtr || l == &nd.Arr[0] || l == &nd.Arr[1] {
			return true
		}
		for i := range nd.Leaves {
			if l == &nd.Leaves[i] {
				return true
			}
		}
	}
	return false
}

func (b *graphBuilder) node() *graphNode {
	return b.nodes[b.r.Intn(len(b.nodes))]
}

// build builds a graph of n nodes and returns its root.
func (b *graphBuilder) build(n int) *graphNode {
	r := b.r
	b.nodes = make([]*graphNode, n)
	for i := range b.nodes {
		nd := &graphNode{Leaf: b.leaf(), Val: int32(r.Intn(1000) - 500), Box: r.Int63()}
		for j := range nd.Ints {
			if r.Intn(2) == 0 {
				nd.Ints[j] = r.Int63() - r.Int63()
			}
		}
		for j := range nd.Arr {
			nd.Arr[j] = b.leaf()
		}
		if r.Intn(2) == 0 {
			nd.Leaves = make([]graphLeaf, r.Intn(4), 1+r.Intn(4)+3)
			for j := range nd.Leaves {
				nd.Leaves[j] = b.leaf()
			}
		}
		switch r.Intn(3) {
		case 0:
			nd.Bytes = []byte{}
		case 1:
			nd.Bytes = make([]byte, r.Intn(20))
			r.Read(nd.Bytes)
		}
		if r.Intn(3) == 0 {
			nd.Names = graphNames{graphName(r.Intn(10)), 0, graphName(r.Intn(10))}
		}
		if r.Intn(2) == 0 {
			nd.FB = make(foreign.Bytes, r.Intn(5), 5)
			r.Read(nd.FB)
		}
		if r.Intn(2) == 0 {
			r.Read(nd.FA[:])
		}
		if r.Intn(2) == 0 {
			nd.FI = foreign.Ints{r.Int31(), 0}
		}
		b.nodes[i] = nd
	}
	for i, nd := range b.nodes {
		if r.Intn(2) == 0 {
			nd.Next = b.node()
		}
		if j := i + 1 + r.Intn(len(b.nodes)); j < len(b.nodes) {
			// Waits must not form cycles.
			nd.W = b.nodes[j]
		}
		switch r.Intn(5) {
		case 0:
			nd.LeafPtr = &b.node().Leaf
		case 1:
			o := b.node()
			nd.LeafPtr = &o.Arr[r.Intn(len(o.Arr))]
		case 2:
			if o := b.node(); len(o.Leaves) > 0 {
				nd.LeafPtr = &o.Leaves[r.Intn(len(o.Leaves))]
			}
		case 3:
			nd.LeafPtr = &graphLeaf{F: 7}
		}
		switch r.Intn(3) {
		case 0:
			o := b.node()
			nd.IntPtr = &o.Ints[r.Intn(len(o.Ints))]
		case 1:
			v := r.Int63()
			nd.IntPtr = &v
		}
		if r.Intn(4) == 0 {
			nd.ArrPtr = &b.node().Ints
		}
		if k := r.Intn(4); k > 0 {
			nd.Kids = make([]*graphNode, k)
			for j := range nd.Kids {
				if r.Intn(4) != 0 {
					nd.Kids[j] = b.node()
				}
			}
			if r.Intn(3) == 0 {
				nd.Kids = nd.Kids[r.Intn(k):]
			}
		}
		if o := b.node(); o != nd && len(o.Leaves) > 1 && r.Intn(3) == 0 {
			// Share another node's slice. Save does not support slices of
			// a backing array at different offsets, and zeroes the elements
			// of a backing array beyond the length of a slice.
			nd.Leaves = o.Leaves
		}
		if r.Intn(5) == 0 {
			// Slice an array field of another node.
			nd.Leaves = b.node().Arr[:]
		}
		if o := b.node(); o != nd && len(o.Bytes) > 2 && r.Intn(3) == 0 {
			nd.Bytes = o.Bytes
		}
		switch r.Intn(6) {
		case 0:
			nd.Any = b.node()
		case 1:
			nd.Any = &b.node().Leaf
		case 2:
			nd.Any = int(r.Intn(100))
		case 3:
			nd.Any = (*graphNode)(nil)
		}
		switch r.Intn(3) {
		case 0:
			nd.Iface = b.node()
		case 1:
			nd.Iface = &b.node().Arr[1]
		}
		if k := r.Intn(4); k > 0 {
			if b.seed%2 == 0 {
				k = 1 // Keep encodings deterministic.
			}
			nd.M = make(map[int32]*graphNode)
			for j := 0; j < k; j++ {
				nd.M[int32(r.Intn(10))] = b.node()
			}
			if len(nd.M) > 1 {
				b.hasMaps = true
			}
		}
		if r.Intn(4) == 0 {
			nd.MS = map[graphName]graphLeaf{graphName(r.Intn(5)): b.leaf()}
		}
		for j := range nd.Links {
			if r.Intn(2) == 0 {
				v := r.Int63()
				nd.Links[j].I = &v
			}
			if r.Intn(2) == 0 {
				nd.Links[j].N = b.node()
			}
		}
		if r.Intn(2) == 0 {
			nd.LinkS = make([]graphLink, 1+r.Intn(3))
			for j := range nd.LinkS {
				v := r.Int63()
				nd.LinkS[j] = graphLink{N: b.node(), I: &v}
			}
		}
		// Refer to links in other nodes, which may be found (and their
		// pointers followed) before the containing node or slice.
		switch r.Intn(3) {
		case 0:
			nd.LinkPtr = &b.node().Links[r.Intn(2)]
		case 1:
			if o := b.node(); len(o.LinkS) > 0 {
				nd.LinkPtr = &o.LinkS[r.Intn(len(o.LinkS))]
			}
		}
	}
	return b.nodes[0]
}

// diff describes the first difference between a and b.
func diff(a, b []byte) string {
	i := 0
	for i < len(a) && i < len(b) && a[i] == b[i] {
		i++
	}
	window := func(x []byte) []byte {
		return x[max(0, i-16):min(len(x), i+16)]
	}
	return fmt.Sprintf("lengths %d and %d, first difference at %d: %v vs %v", len(a), len(b), i, window(a), window(b))
}

// TestCodecRandomGraphs checks the generated codecs against the
// reflection-based implementation on random object graphs: each decoder must
// reproduce the graph, including pointer aliasing (checked by comparing the
// reflection-based encodings of the original and loaded graphs, when they do
// not depend on map iteration order), and each struct must be saved once.
func TestCodecRandomGraphs(t *testing.T) {
	seeds := 500
	if testing.Short() {
		seeds = 50
	}
	for seed := 0; seed < seeds; seed++ {
		b := graphBuilder{seed: seed, r: rand.New(rand.NewSource(int64(seed)))}
		root := b.build(1 + seed%17)
		var encoded [2][]byte
		for i, codecs := range []bool{false, true} {
			clear(graphSaves)
			data, _, err := save(root, codecs)
			if err != nil {
				t.Fatalf("seed %d: save (codecs=%t) failed: %v", seed, codecs, err)
			}
			for p, n := range graphSaves {
				if l, ok := p.(*graphLeaf); ok && !b.ownsLeaf(l) {
					// Map values are saved from copies, whose
					// addresses may be reused.
					continue
				}
				if n != 1 {
					t.Fatalf("seed %d: save (codecs=%t) saved %T %p %d times", seed, codecs, p, p, n)
				}
			}
			encoded[i] = data
		}
		reflectData, codecData := encoded[0], encoded[1]
		for _, enc := range []struct {
			name string
			data []byte
		}{{"reflect", reflectData}, {"codec", codecData}} {
			for _, codecs := range []bool{false, true} {
				if enc.name == "codec" && !codecs {
					// The typed format requires codecs.
					continue
				}
				loaded, _, err := load(reflect.TypeOf(root), enc.data, codecs)
				if err != nil {
					t.Fatalf("seed %d: load (codecs=%t) of %s encoding failed: %v", seed, codecs, enc.name, err)
				}
				got := loaded.(*graphNode)
				// Loaded counts afterLoad calls; it is not saved.
				for _, nd := range b.nodes {
					nd.Loaded = 1
				}
				if !reflect.DeepEqual(root, got) {
					t.Fatalf("seed %d: load (codecs=%t) of %s encoding differs", seed, codecs, enc.name)
				}
				for _, nd := range b.nodes {
					nd.Loaded = 0
				}
				// Saving the loaded graph must give the same encoding,
				// which checks that aliasing was preserved.
				again, _, err := save(got, false)
				if err != nil {
					t.Fatalf("seed %d: saving loaded graph failed: %v", seed, err)
				}
				if !b.hasMaps && !bytes.Equal(again, reflectData) {
					t.Fatalf("seed %d: load (codecs=%t) of %s encoding: re-encoding differs (maps %t): %s", seed, codecs, enc.name, b.hasMaps, diff(reflectData, again))
				}
			}
		}
	}
}

// TestCodecAbsorbedReferrer checks an object whose only referrer is merged
// into a containing object after referring to it.
func TestCodecAbsorbedReferrer(t *testing.T) {
	v := int64(42)
	arr := &[2]graphLink{{}, {I: &v}}
	// Saving root finds arr[1] first, and follows its pointer to v, before
	// finding arr through root.Next.Next.
	root := &graphNode{LinkPtr: &arr[1], Next: &graphNode{Next: &graphNode{LinkS: arr[:]}}}
	data, _, err := save(root, true)
	if err != nil {
		t.Fatalf("save failed: %v", err)
	}
	loaded, _, err := load(reflect.TypeOf(root), data, true)
	if err != nil {
		t.Fatalf("load failed: %v", err)
	}
	got := loaded.(*graphNode)
	if got.LinkPtr == nil || got.LinkPtr.I == nil || *got.LinkPtr.I != 42 {
		t.Fatalf("got LinkPtr %+v, want a link to 42", got.LinkPtr)
	}
	if got.LinkPtr != &got.Next.Next.LinkS[1] {
		t.Errorf("LinkPtr does not point into Next.Next.LinkS")
	}
}

// TestCodecStandalone checks a backing array found through a pointer to an
// element, whose slice is only reachable from that element, so that the
// encoder writes it without a preceding reference providing its decoder.
func TestCodecStandalone(t *testing.T) {
	o := &graphNode{}
	o.LinkS = []graphLink{{N: o}}
	root := &graphNode{LinkPtr: &o.LinkS[0]}
	data, _, err := save(root, true)
	if err != nil {
		t.Fatalf("save failed: %v", err)
	}
	loaded, _, err := load(reflect.TypeOf(root), data, true)
	if err != nil {
		t.Fatalf("load failed: %v", err)
	}
	got := loaded.(*graphNode)
	if got.LinkPtr == nil || got.LinkPtr.N == nil {
		t.Fatalf("got LinkPtr %+v, want a link to a node", got.LinkPtr)
	}
	if n := got.LinkPtr.N; len(n.LinkS) != 1 || &n.LinkS[0] != got.LinkPtr || n.LinkS[0].N != n {
		t.Errorf("got node %+v, want LinkS[0] at LinkPtr referring to the node", n)
	}
}

// TestCodecNestedMaps checks maps whose values are maps.
func TestCodecNestedMaps(t *testing.T) {
	root := &nestedMaps{M: map[int]map[int32]struct{}{0: {1: {}, 5: {}}, 15: {7: {}}, 3: {}}}
	data, _, err := save(root, true)
	if err != nil {
		t.Fatalf("save failed: %v", err)
	}
	loaded, _, err := load(reflect.TypeOf(root), data, true)
	if err != nil {
		t.Fatalf("load failed: %v", err)
	}
	if got := loaded.(*nestedMaps); !reflect.DeepEqual(got, root) {
		t.Errorf("got %v, want %v", got.M, root.M)
	}
}

// TestCodecByteArrayInitialization compares the documented zero-value load
// behavior through both encoders in the same binary. Unlike load(), this uses
// the existing root directly instead of allocating a fresh destination.
func TestCodecByteArrayInitialization(t *testing.T) {
	var _ state.Codec = (*initializedByteArrayRoot)(nil)
	for _, mode := range []struct {
		name   string
		codecs bool
	}{{"legacy", false}, {"typed", true}} {
		t.Run(mode.name, func(t *testing.T) {
			withCodecs(mode.codecs, func() {
				for _, tc := range []struct {
					name                 string
					saved, initial, want byte
				}{
					{"zero_into_initialized", 0, 7, 7},
					{"nonzero_into_initialized", 9, 7, 9},
					{"zero_into_zero", 0, 0, 0},
				} {
					t.Run(tc.name, func(t *testing.T) {
						source := initializedByteArrayRoot{A: [1]byte{tc.saved}}
						var buf bytes.Buffer
						if _, err := state.Save(context.Background(), &buf, &source); err != nil {
							t.Fatalf("Save: %v", err)
						}
						destination := initializedByteArrayRoot{A: [1]byte{tc.initial}}
						if _, err := state.Load(context.Background(), bytes.NewReader(buf.Bytes()), &destination); err != nil {
							t.Fatalf("Load: %v", err)
						}
						t.Logf("saved=%d initial=%d loaded=%d", tc.saved, tc.initial, destination.A[0])
						if got, want := destination.A[0], tc.want; got != want {
							t.Errorf("loaded byte = %d, want %d", got, want)
						}
					})
				}
			})
		})
	}
}

// TestCodecArrayInitialization distinguishes a field loaded into an existing
// root from arrays in newly allocated objects and separate slice backing.
func TestCodecArrayInitialization(t *testing.T) {
	var _ state.Codec = (*initializedArrays)(nil)
	var _ state.Codec = (*initializedArraySlices)(nil)
	initial := initializedArrays{Bytes: [3]byte{7, 7, 7}, Ints: [3]uint64{7, 7, 7}}
	for _, mode := range codecModes {
		t.Run(mode.name, func(t *testing.T) {
			withCodecs(mode.codecs, func() {
				for _, tc := range []struct {
					name   string
					source initializedArrays
					want   initializedArrays
				}{
					{"zero", initializedArrays{}, initial},
					{"mixed", initializedArrays{Bytes: [3]byte{0, 9, 0}, Ints: [3]uint64{0, 9, 0}}, initializedArrays{Bytes: [3]byte{0, 9, 0}, Ints: [3]uint64{0, 9, 0}}},
				} {
					t.Run(tc.name, func(t *testing.T) {
						t.Run("initialized_root", func(t *testing.T) {
							destination := initial
							roundTripArrays(t, &tc.source, &destination)
							t.Logf("saved=%+v initial=%+v loaded=%+v", tc.source, initial, destination)
							if got, want := destination, tc.want; got != want {
								t.Errorf("loaded arrays = %+v, want %+v", got, want)
							}
						})
						t.Run("allocated_object", func(t *testing.T) {
							source := &tc.source
							var destination *initializedArrays
							roundTripArrays(t, &source, &destination)
							t.Logf("saved=%+v loaded=%+v", tc.source, destination)
							if got, want := destination, &tc.source; !reflect.DeepEqual(got, want) {
								t.Errorf("loaded arrays = %+v, want %+v", got, want)
							}
						})
						t.Run("slice_backing", func(t *testing.T) {
							source := initializedArraySlices{Bytes: append([]byte(nil), tc.source.Bytes[:]...), Ints: append([]uint64(nil), tc.source.Ints[:]...)}
							destination := initializedArraySlices{Bytes: []byte{7, 7, 7}, Ints: []uint64{7, 7, 7}}
							roundTripArrays(t, &source, &destination)
							t.Logf("saved=%+v loaded=%+v", source, destination)
							if got, want := destination, source; !reflect.DeepEqual(got, want) {
								t.Errorf("loaded slices = %+v, want %+v", got, want)
							}
						})
					})
				}
			})
		})
	}
}

// roundTripArrays fragments reads to exercise the bounded wire-reader scratch
// storage as well as the array's own decoding behavior.
func roundTripArrays(t *testing.T, source, destination any) {
	t.Helper()
	var buf bytes.Buffer
	if _, err := state.Save(context.Background(), &buf, source); err != nil {
		t.Fatalf("Save: %v", err)
	}
	if _, err := state.Load(context.Background(), iotest.OneByteReader(bytes.NewReader(buf.Bytes())), destination); err != nil {
		t.Fatalf("Load: %v", err)
	}
}

func TestCodecArrayInitializationContexts(t *testing.T) {
	for _, mode := range codecModes {
		t.Run(mode.name, func(t *testing.T) {
			withCodecs(mode.codecs, func() {
				t.Run("nested", func(t *testing.T) {
					initial := initializedNestedArrays{Values: [2][2]uint64{{7, 7}, {7, 7}}}
					for _, saved := range [][2][2]uint64{{}, {{0, 0}, {0, 9}}} {
						source := initializedNestedArrays{Values: saved}
						destination := initial
						roundTripArrays(t, &source, &destination)
						want := source
						if saved == [2][2]uint64{} {
							want = initial
						}
						if got := destination; got != want {
							t.Errorf("loaded nested arrays = %+v, want %+v", got, want)
						}
					}
				})
				t.Run("dynamic_bytes", func(t *testing.T) {
					for _, nonzero := range []bool{false, true} {
						var source initializedDynamicArrays
						destination := initializedDynamicArrays{Bytes: foreign.Array{7, 7, 7, 7, 7}}
						want := destination
						if nonzero {
							source.Bytes[1] = 9
							want = source
						}
						roundTripArrays(t, &source, &destination)
						if got := destination; got != want {
							t.Errorf("loaded dynamic arrays = %+v, want %+v", got, want)
						}
					}
				})
				t.Run("byte_chunks", func(t *testing.T) {
					for _, nonzero := range []bool{false, true} {
						var source, destination initializedWideBytes
						for i := range destination.Values {
							destination.Values[i] = 7
						}
						want := destination
						if nonzero {
							source.Values[33] = 9
							want = source
						}
						roundTripArrays(t, &source, &destination)
						if got := destination; got != want {
							t.Errorf("loaded byte array = %+v, want %+v", got, want)
						}
					}
				})
				t.Run("hooks_and_nil_references", func(t *testing.T) {
					saved, initial := uint64(9), uint64(7)
					source := initializedArrayContexts{Elements: [2]initializedArrayElement{{}, {Value: 9}}, Pointers: [2]*uint64{nil, &saved}}
					destination := initializedArrayContexts{Elements: [2]initializedArrayElement{{Value: 7}, {Value: 7}}, Pointers: [2]*uint64{&initial, &initial}}
					roundTripArrays(t, &source, &destination)
					if got, want := destination.Elements, [2]initializedArrayElement{{Value: 7, Loaded: 1}, {Value: 9, Loaded: 1}}; got != want {
						t.Errorf("loaded elements = %+v, want %+v", got, want)
					}
					if got, want := destination.Pointers[0], &initial; got != want {
						t.Errorf("nil reference replaced initialized pointer: got %p, want %p", got, want)
					}
					if got := destination.Pointers[1]; got == nil || *got != saved {
						t.Errorf("loaded nonnil pointer = %v, want pointer to %d", got, saved)
					}
				})
				t.Run("slice_aliases", func(t *testing.T) {
					for _, nonzero := range []bool{false, true} {
						var source initializedArrayAliases
						destination := initializedArrayAliases{Bytes: [3]byte{7, 7, 7}, Ints: [3]uint64{7, 7, 7}}
						wantBytes, wantInts := destination.Bytes, destination.Ints
						if nonzero {
							source.Bytes[1], source.Ints[1] = 9, 9
							wantBytes, wantInts = source.Bytes, source.Ints
						}
						source.ByteSlice, source.IntSlice = source.Bytes[:], source.Ints[:]
						destination.ByteSlice, destination.IntSlice = destination.Bytes[:], destination.Ints[:]
						roundTripArrays(t, &source, &destination)
						if got, want := destination.Bytes, wantBytes; got != want {
							t.Errorf("loaded bytes = %v, want %v", got, want)
						}
						if got, want := destination.Ints, wantInts; got != want {
							t.Errorf("loaded ints = %v, want %v", got, want)
						}
						if got, want := len(destination.ByteSlice), len(destination.Bytes); got != want {
							t.Fatalf("byte slice length = %d, want %d", got, want)
						}
						if got, want := len(destination.IntSlice), len(destination.Ints); got != want {
							t.Fatalf("int slice length = %d, want %d", got, want)
						}
						if &destination.ByteSlice[0] != &destination.Bytes[0] || &destination.IntSlice[0] != &destination.Ints[0] {
							t.Error("loaded slices do not alias their array fields")
						}
					}
				})
				t.Run("nested_signed_zero", func(t *testing.T) {
					negativeZero := math.Copysign(0, -1)
					source := initializedNestedFloats{
						Values: [2][1]float64{{negativeZero}, {9}},
						Map:    map[int][1]float64{1: {negativeZero}},
						Slice:  [][1]float64{{negativeZero}},
					}
					destination := initializedNestedFloats{Values: [2][1]float64{{7}, {7}}}
					roundTripArrays(t, &source, &destination)
					if got, want := len(destination.Slice), 1; got != want {
						t.Fatalf("nested slice length = %d, want %d", got, want)
					}
					for name, value := range map[string]float64{"array": destination.Values[0][0], "map": destination.Map[1][0]} {
						if got, want := math.Float64bits(value), math.Float64bits(negativeZero); got != want {
							t.Errorf("%s nested zero bits = %#x, want %#x", name, got, want)
						}
					}
					// The separate backing array is wholly zero and is elided.
					if got, want := math.Float64bits(destination.Slice[0][0]), uint64(0); got != want {
						t.Errorf("slice nested zero bits = %#x, want %#x", got, want)
					}
					if got, want := destination.Values[1][0], float64(9); got != want {
						t.Errorf("nested nonzero value = %v, want %v", got, want)
					}
				})
				t.Run("signed_zero", func(t *testing.T) {
					negativeZero := math.Copysign(0, -1)
					for _, initial := range [][2]float64{{}, {7, 7}} {
						for _, mixed := range []bool{false, true} {
							source := initializedFloatArrays{Values: [2]float64{negativeZero, 0}, Slice: []float64{negativeZero, 0}}
							want := initial
							if mixed {
								source.Values[1], source.Slice[1] = 9, 9
								want = source.Values
							}
							destination := initializedFloatArrays{Values: initial, Slice: []float64{7, 7}}
							roundTripArrays(t, &source, &destination)
							for i := range want {
								if got, want := math.Float64bits(destination.Values[i]), math.Float64bits(want[i]); got != want {
									t.Errorf("loaded float bits[%d] = %#x, want %#x", i, got, want)
								}
							}
							if got, want := len(destination.Slice), len(source.Slice); got != want {
								t.Fatalf("float slice length = %d, want %d", got, want)
							}
							wantSlice := [2]float64{}
							if mixed {
								wantSlice = source.Values
							}
							for i := range wantSlice {
								if got, want := math.Float64bits(destination.Slice[i]), math.Float64bits(wantSlice[i]); got != want {
									t.Errorf("loaded slice bits[%d] = %#x, want %#x", i, got, want)
								}
							}
						}
					}
				})
			})
		})
	}
}

// TestCodecStreamCompatibility is also the untimed cross-binary gate for the
// format experiment. The runner first saves each implementation's stream, then
// asks the other implementation to reject it before modifying the root.
func TestCodecStreamCompatibility(t *testing.T) {
	negativeZero := math.Copysign(0, -1)
	source := initializedNestedFloats{
		Values: [2][1]float64{{negativeZero}, {9}},
		Map:    map[int][1]float64{1: {negativeZero}},
		Slice:  [][1]float64{{negativeZero}, {9}},
	}
	var buf bytes.Buffer
	if _, err := state.Save(context.Background(), &buf, &source); err != nil {
		t.Fatalf("Save: %v", err)
	}
	observed := struct {
		Mode   string         `json:"mode"`
		Bytes  int            `json:"bytes"`
		SHA256 string         `json:"sha256"`
		Error  string         `json:"error,omitempty"`
		Sizes  map[string]int `json:"sizes"`
	}{Mode: "self", Bytes: buf.Len(), SHA256: fmt.Sprintf("%x", sha256.Sum256(buf.Bytes())), Sizes: make(map[string]int)}
	for _, name := range []string{"fields", "bytes", "arrays", "arrays_zero", "floats", "floats_zero", "nested", "nested_zero", "array_maps", "array_maps_zero"} {
		var encoded bytes.Buffer
		if _, err := state.Save(context.Background(), &encoded, allObjects[name].New(1024)); err != nil {
			t.Fatalf("Save %s: %v", name, err)
		}
		observed.Sizes[name] = encoded.Len()
	}
	destination := initializedNestedFloats{Values: [2][1]float64{{7}, {7}}}
	if filename := os.Getenv("STATE_CODEC_STREAM_INPUT"); filename != "" {
		data, err := os.ReadFile(filename)
		if err != nil {
			t.Fatalf("Read peer stream: %v", err)
		}
		initial := destination
		_, err = state.Load(context.Background(), bytes.NewReader(data), &destination)
		if err == nil || !strings.Contains(err.Error(), "incompatible version") {
			t.Fatalf("Load peer stream = %v, want incompatible version", err)
		}
		if !reflect.DeepEqual(destination, initial) {
			t.Fatalf("rejected stream changed root: got %+v, want %+v", destination, initial)
		}
		observed.Mode, observed.Error = "reject", err.Error()
	} else {
		if _, err := state.Load(context.Background(), bytes.NewReader(buf.Bytes()), &destination); err != nil {
			t.Fatalf("Load own stream: %v", err)
		}
		if got, want := len(destination.Slice), 2; got != want {
			t.Fatalf("slice length = %d, want %d", got, want)
		}
		for name, value := range map[string]float64{"field": destination.Values[0][0], "map": destination.Map[1][0], "slice": destination.Slice[0][0]} {
			if got, want := math.Float64bits(value), math.Float64bits(negativeZero); got != want {
				t.Errorf("%s zero bits = %#x, want %#x", name, got, want)
			}
		}
		if got, want := destination.Values[1][0], float64(9); got != want {
			t.Errorf("nonzero field = %v, want %v", got, want)
		}
	}
	if output := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR"); output != "" {
		if err := os.WriteFile(filepath.Join(output, "codec-stream.bin"), buf.Bytes(), 0600); err != nil {
			t.Fatalf("Write stream: %v", err)
		}
		data, err := json.MarshalIndent(observed, "", "  ")
		if err != nil {
			t.Fatalf("Marshal observations: %v", err)
		}
		if err := os.WriteFile(filepath.Join(output, "codec-stream.json"), data, 0600); err != nil {
			t.Fatalf("Write observations: %v", err)
		}
	}
}

var _ state.Codec = (*foreignArrayValues)(nil)

// TestCodecForeignArrayValues requires full array/map values to retain signed
// zeros, while a wholly zero struct field leaves initialized storage intact.
func TestCodecForeignArrayValues(t *testing.T) {
	negativeZero := math.Copysign(0, -1)
	for _, mode := range codecModes {
		t.Run(mode.name, func(t *testing.T) {
			for _, test := range []struct {
				name  string
				value foreign.FloatArray
			}{
				{"positive_zero", foreign.FloatArray{0, 0}},
				{"negative_zero", foreign.FloatArray{negativeZero, 0}},
				{"mixed", foreign.FloatArray{negativeZero, 9}},
			} {
				t.Run(test.name, func(t *testing.T) {
					for _, location := range []string{"map", "array", "initialized_array", "field"} {
						t.Run(location, func(t *testing.T) {
							root := foreignArrayValues{Neighbor: 42}
							dst := foreignArrayValues{Neighbor: 7}
							want := test.value
							switch location {
							case "map":
								root.Map = map[int]foreign.FloatArray{0: test.value, 1: {9, 9}}
							case "array", "initialized_array":
								root.Array = [2]foreign.FloatArray{test.value, {9, 9}}
								if location == "initialized_array" {
									dst.Array = [2]foreign.FloatArray{{7, 7}, {7, 7}}
								}
							case "field":
								root.Field = test.value
								dst.Field = foreign.FloatArray{7, 7}
								if test.value == (foreign.FloatArray{}) {
									want = dst.Field
								}
							}
							withCodecs(mode.codecs, func() {
								var stream bytes.Buffer
								if _, err := state.Save(context.Background(), &stream, &root); err != nil {
									t.Fatalf("Save: %v", err)
								}
								if _, err := state.Load(context.Background(), &stream, &dst); err != nil {
									t.Fatalf("Load: %v", err)
								}
							})
							if got, want := dst.Neighbor, root.Neighbor; got != want {
								t.Errorf("Neighbor = %d, want %d", got, want)
							}
							var got foreign.FloatArray
							switch location {
							case "map":
								if got, want := len(dst.Map), 2; got != want {
									t.Fatalf("len(Map) = %d, want %d", got, want)
								}
								if got, want := dst.Map[1], (foreign.FloatArray{9, 9}); got != want {
									t.Fatalf("Map[1] = %v, want %v", got, want)
								}
								var ok bool
								got, ok = dst.Map[0]
								if !ok {
									t.Fatal("Map is missing key 0")
								}
							case "array", "initialized_array":
								if got, want := dst.Array[1], (foreign.FloatArray{9, 9}); got != want {
									t.Fatalf("Array[1] = %v, want %v", got, want)
								}
								got = dst.Array[0]
							case "field":
								got = dst.Field
							}
							for i := range want {
								if got, want := math.Float64bits(got[i]), math.Float64bits(want[i]); got != want {
									t.Errorf("element %d bits = %#x, want %#x", i, got, want)
								}
							}
						})
					}
				})
			}
		})
	}
}
