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
	"math"
	"reflect"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/state"
)

func TestTypedSnapshotWireAndAliases(t *testing.T) {
	makeGraph := func() *system3 {
		parent := &outerFieldFirst{inner: inner{v: -7}, v: 11}
		custom := &innerFieldValue{v: 19}
		return &system3{
			// Discover the interior object before its containing struct.
			v1: &parent.inner,
			v2: parent,
			v3: &system{v1: custom, v2: custom},
		}
	}
	var snapshots bytes.Buffer
	graph := makeGraph()
	if _, err := state.Save(t.Context(), &snapshots, &graph); err != nil {
		t.Fatal(err)
	}
	// Retain the original stream for the cross-binary compatibility check.
	t.Logf("mixed-graph-wire: %x", snapshots.Bytes())
	var loaded *system3
	if _, err := state.Load(t.Context(), &snapshots, &loaded); err != nil {
		t.Fatal(err)
	}
	parent := loaded.v2.(*outerFieldFirst)
	if got, want := loaded.v1.(*inner), &parent.inner; got != want {
		t.Fatalf("interior alias = %p, want %p", got, want)
	}
	custom := loaded.v3.(*system)
	if got, want := custom.v1.(*innerFieldValue), custom.v2.(*innerFieldValue); got != want {
		t.Fatalf("custom alias = %p, want %p", got, want)
	}
	if got, want := custom.v1.(*innerFieldValue).v, int64(19); got != want {
		t.Fatalf("custom value = %d, want %d", got, want)
	}
}

func TestNativeRegionLateParent(t *testing.T) {
	for _, name := range []string{"alias", "nil", "standalone"} {
		t.Run(name, func(t *testing.T) {
			parent := &nativeRegionParent{child: nativeRegionChild{value: 37}, value: 129}
			parent.self = parent
			graph := nativeRegionGraph{child: &parent.child, parent: parent}
			if name == "standalone" {
				graph.parent = nil
				graph.child = &nativeRegionChild{value: 37}
			}
			if name != "nil" {
				graph.named = nativeRegionChildPointer(graph.child)
			}
			var encoded bytes.Buffer
			if _, err := state.Save(t.Context(), &encoded, &graph); err != nil {
				t.Fatal(err)
			}
			// A late parent replaces the child before encoding and omits its
			// field. Discovery must not emit that unused type definition.
			if got, want := bytes.Contains(encoded.Bytes(), []byte(graph.child.StateTypeName())), name == "standalone"; got != want {
				t.Fatalf("child type definition present = %t, want %t", got, want)
			}
			t.Logf("native-region-wire[%s]: %x", name, encoded.Bytes())
			var loaded nativeRegionGraph
			if _, err := state.Load(t.Context(), &encoded, &loaded); err != nil {
				t.Fatal(err)
			}
			if name == "nil" {
				if loaded.named != nil {
					t.Fatalf("defined pointer = %p, want nil", loaded.named)
				}
			} else if loaded.named != nativeRegionChildPointer(loaded.child) {
				t.Fatal("defined and ordinary pointers lost their alias")
			}
			if name == "standalone" {
				if loaded.parent != nil || loaded.child == nil {
					t.Fatalf("standalone child changed: %#v", loaded)
				}
				if got, want := loaded.child.value, 37; got != want {
					t.Fatalf("child value = %d, want %d", got, want)
				}
				return
			}
			if loaded.parent == nil || loaded.child != &loaded.parent.child || loaded.parent.self != loaded.parent {
				t.Fatalf("late-parent or cycle identity lost: %#v", loaded)
			}
			if got, want := loaded.child.value, 0; got != want {
				t.Errorf("child value = %d, want %d", got, want)
			}
			if got, want := loaded.parent.value, 129; got != want {
				t.Errorf("parent value = %d, want %d", got, want)
			}
		})
	}
}

// TestGeneratedPrimitiveRecord covers checked field types and floating-point
// values whose representation must survive generated capture and emission.
func TestGeneratedPrimitiveRecord(t *testing.T) {
	runTestCases(t, false, "import_alias", []any{&statewire{value: -19}})
	for _, test := range []struct {
		name  string
		value primitiveRecord
	}{
		{name: "zero"},
		{name: "nonzero", value: primitiveRecord{
			signed:        -32767,
			unsigned:      1<<63 + 17,
			imported:      -17 * time.Second,
			text:          "saved primitive fields",
			enabled:       true,
			narrow:        1.25,
			wide:          -1.0000000000000002,
			narrowComplex: complex(-2.5, 7.75),
			wideComplex:   complex(1.0000000000000002, -2.0000000000000004),
		}},
		{name: "negative_zero", value: primitiveRecord{
			narrow:        float32(math.Copysign(0, -1)),
			wide:          math.Copysign(0, -1),
			narrowComplex: complex(float32(math.Copysign(0, -1)), 1),
			wideComplex:   complex(1, math.Copysign(0, -1)),
		}},
		{name: "signaling_nan", value: primitiveRecord{
			narrow:        math.Float32frombits(0x7f800001),
			wide:          math.Float64frombits(0x7ff0000000000001),
			narrowComplex: complex(math.Float32frombits(0xff800001), math.Float32frombits(0x7fc12345)),
			wideComplex:   complex(math.Float64frombits(0xfff0000000000001), math.Float64frombits(0x7ff8000000001234)),
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			var encoded bytes.Buffer
			if _, err := state.Save(t.Context(), &encoded, &test.value); err != nil {
				t.Fatal(err)
			}
			t.Logf("primitive-record-wire[%s]: %x", test.name, encoded.Bytes())
			var loaded primitiveRecord
			if _, err := state.Load(t.Context(), &encoded, &loaded); err != nil {
				t.Fatal(err)
			}
			if test.name == "negative_zero" {
				if got, want := math.Float32bits(real(loaded.narrowComplex)), uint32(1<<31); got != want {
					t.Errorf("complex64 real bits = %#x, want %#x", got, want)
				}
				if got, want := math.Float64bits(imag(loaded.wideComplex)), uint64(1<<63); got != want {
					t.Errorf("complex128 imaginary bits = %#x, want %#x", got, want)
				}
			}
			if test.name == "signaling_nan" {
				for _, value := range []float64{float64(loaded.narrow), loaded.wide, float64(real(loaded.narrowComplex)), float64(imag(loaded.narrowComplex)), real(loaded.wideComplex), imag(loaded.wideComplex)} {
					if !math.IsNaN(value) {
						t.Errorf("loaded floating value = %v, want NaN", value)
					}
				}
			} else if !reflect.DeepEqual(loaded, test.value) {
				t.Errorf("loaded record = %#v, want %#v", loaded, test.value)
			}
		})
	}
}
