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

package tests

import (
	"bytes"
	"context"
	"math/rand"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
)

func TestEmptyStruct(t *testing.T) {
	runTestCases(t, false, "plain", []any{
		unregisteredEmptyStruct{},
		typeOnlyEmptyStruct{},
		savableEmptyStruct{},
	})
	runTestCases(t, false, "pointers", pointersTo([]any{
		unregisteredEmptyStruct{},
		typeOnlyEmptyStruct{},
		savableEmptyStruct{},
	}))
	runTestCases(t, false, "interfaces-pass", interfacesTo([]any{
		// Only registered types can be dispatched via interfaces. All
		// other types should fail, even if it is the empty struct.
		savableEmptyStruct{},
	}))
	runTestCases(t, true, "interfaces-fail", interfacesTo([]any{
		unregisteredEmptyStruct{},
		typeOnlyEmptyStruct{},
	}))
	runTestCases(t, false, "interfacesToPointers-pass", interfacesTo(pointersTo([]any{
		savableEmptyStruct{},
	})))
	runTestCases(t, true, "interfacesToPointers-fail", interfacesTo(pointersTo([]any{
		unregisteredEmptyStruct{},
		typeOnlyEmptyStruct{},
	})))

	// Ensuring empty struct aliasing works.
	es := emptyStructPointer{new(struct{})}
	runTestCases(t, false, "empty-struct-pointers", []any{
		emptyStructPointer{},
		es,
		[]emptyStructPointer{es, es}, // Same pointer.
	})
}

func TestEmbeddedPointers(t *testing.T) {
	// Give each int64 a random value to prevent Go from using
	// runtime.staticuint64s, which confounds tests for struct duplication.
	magic := func() int64 {
		for {
			n := rand.Int63()
			if n < 0 || n > 255 {
				return n
			}
		}
	}

	ofs := outerSame{inner{magic()}}
	of1 := outerFieldFirst{inner{magic()}, magic()}
	of2 := outerFieldSecond{magic(), inner{magic()}}
	oa := outerArray{[2]inner{{magic()}, {magic()}}}
	osl := outerSlice{oa.inner[:]}
	ofv := outerFieldValue{innerFieldValue{magic()}}

	runTestCases(t, false, "embedded-pointers", []any{
		system{&ofs, &ofs.inner},
		system{&ofs.inner, &ofs},
		system{&of1, &of1.inner},
		system{&of1.inner, &of1},
		system{&of2, &of2.inner},
		system{&of2.inner, &of2},
		system{&oa, &oa.inner[0]},
		system{&oa, &oa.inner[1]},
		system{&oa.inner[0], &oa},
		system{&oa.inner[1], &oa},
		system3{&oa, &oa.inner[0], &oa.inner[1]},
		system3{&oa, &oa.inner[1], &oa.inner[0]},
		system3{&oa.inner[0], &oa, &oa.inner[1]},
		system3{&oa.inner[1], &oa, &oa.inner[0]},
		system3{&oa.inner[0], &oa.inner[1], &oa},
		system3{&oa.inner[1], &oa.inner[0], &oa},
		system{&oa, &osl},
		system{&osl, &oa},
		system{&ofv, &ofv.inner},
		system{&ofv.inner, &ofv},
	})
}

func TestMultiNameFields(t *testing.T) {
	runTestCases(t, false, "multi-name-field", []any{
		multiName{b: "foo", c: "bar", x: 10, y: 20, z: -30},
	})
}

func TestSnapshotEmbedding(t *testing.T) {
	// A child's encoder must not replace the parent's complete representation,
	// or dereference an embedding chain that ordinary Save preserves as nil.
	runTestCases(t, false, "embedding", []any{
		snapshotEmbedded{inner: inner{v: 7}, parent: 11},
		snapshotNilEmbedded{snapshotPointerEmbedded: nil, parent: 19},
	})
}

func TestStructSaveIdentity(t *testing.T) {
	for _, test := range []struct {
		name      string
		makeGraph func(*int) *system
		want      int
	}{
		{
			name: "late_parent",
			makeGraph: func(calls *int) *system {
				parent := &countedSaveParent{child: countedSave{value: 17, calls: calls}, value: 23}
				// The intermediary is deferred after the child. Its save then
				// discovers a parent containing that already captured child.
				return &system{v1: &parent.child, v2: &system{v1: parent}}
			},
			want: 1,
		},
		{
			name: "shared_value",
			makeGraph: func(calls *int) *system {
				var value any = countedSave{value: 17, calls: calls}
				return &system{v1: value, v2: value}
			},
			want: 1,
		},
		{
			name: "equal_values",
			makeGraph: func(calls *int) *system {
				value := countedSave{value: 17, calls: calls}
				return &system{v1: value, v2: value}
			},
			want: 2,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			var calls int
			graph := test.makeGraph(&calls)
			var encoded bytes.Buffer
			if _, err := state.Save(t.Context(), &encoded, &graph); err != nil {
				t.Fatal(err)
			}
			if got, want := calls, test.want; got != want {
				t.Fatalf("save calls = %d, want %d", got, want)
			}
			var loaded *system
			if _, err := state.Load(t.Context(), &encoded, &loaded); err != nil {
				t.Fatal(err)
			}
			if test.name == "late_parent" {
				parent := loaded.v2.(*system).v1.(*countedSaveParent)
				if got, want := loaded.v1.(*countedSave), &parent.child; got != want {
					t.Fatalf("interior alias = %p, want %p", got, want)
				}
				if got, want := parent.child.value, int64(17); got != want {
					t.Errorf("child value = %d, want %d", got, want)
				}
				if got, want := parent.value, int64(23); got != want {
					t.Errorf("parent value = %d, want %d", got, want)
				}
			} else {
				for _, value := range []any{loaded.v1, loaded.v2} {
					if got, want := value.(countedSave).value, int64(17); got != want {
						t.Errorf("value = %d, want %d", got, want)
					}
				}
			}
		})
	}
}

func TestStructSnapshotOwnership(t *testing.T) {
	for _, test := range []struct {
		name      string
		makeGraph func(*int) *system
		check     func(*testing.T, *system)
	}{
		{
			name: "active_child",
			makeGraph: func(calls *int) *system {
				parent := &countedSaveParent{child: countedSave{value: 17, calls: calls}, value: 23}
				// The child's saver discovers its parent while its own snapshot
				// is still being filled, before the child leaves the call stack.
				parent.child.parent = parent
				return &system{v1: &parent.child}
			},
			check: func(t *testing.T, graph *system) {
				child := graph.v1.(*countedSave)
				if got, want := child.value, int64(17); got != want {
					t.Errorf("child value = %d, want %d", got, want)
				}
				if got, want := &child.parent.child, child; got != want {
					t.Errorf("parent's child = %p, want %p", got, want)
				}
				if got, want := child.parent.value, int64(23); got != want {
					t.Errorf("parent value = %d, want %d", got, want)
				}
			},
		},
		{
			name: "successive_parents",
			makeGraph: func(calls *int) *system {
				grandparent := &countedSaveGrandparent{parent: countedSaveParent{
					child: countedSave{value: 17, calls: calls}, value: 23,
				}}
				// The intermediary discovers both containing objects after the
				// child is captured, replacing the uncaptured parent immediately.
				// Parent and grandparent occupy the same address range.
				return &system{v1: &grandparent.parent.child, v2: &system{
					v1: &grandparent.parent, v2: grandparent,
				}}
			},
			check: func(t *testing.T, graph *system) {
				parents := graph.v2.(*system)
				parent := parents.v1.(*countedSaveParent)
				grandparent := parents.v2.(*countedSaveGrandparent)
				if got, want := parent, &grandparent.parent; got != want {
					t.Errorf("parent = %p, want %p", got, want)
				}
				if got, want := graph.v1.(*countedSave), &parent.child; got != want {
					t.Errorf("child = %p, want %p", got, want)
				}
				if got, want := parent.child.value, int64(17); got != want {
					t.Errorf("child value = %d, want %d", got, want)
				}
				if got, want := parent.value, int64(23); got != want {
					t.Errorf("parent value = %d, want %d", got, want)
				}
			},
		},
		{
			name: "array_parent",
			makeGraph: func(calls *int) *system {
				array := &[1]countedSave{{value: 17, calls: calls}}
				return &system{v1: &array[0], v2: &system{v1: array}}
			},
			check: func(t *testing.T, graph *system) {
				array := graph.v2.(*system).v1.(*[1]countedSave)
				if got, want := graph.v1.(*countedSave), &array[0]; got != want {
					t.Errorf("array element = %p, want %p", got, want)
				}
				if got, want := array[0].value, int64(17); got != want {
					t.Errorf("element value = %d, want %d", got, want)
				}
			},
		},
		{
			name: "inline_before_pointer",
			makeGraph: func(calls *int) *system {
				child := &countedSave{value: 17, calls: calls}
				return &system{v1: &manualInlineSave{value: child}, v2: &system{v1: child}}
			},
			check: func(t *testing.T, graph *system) {
				inline := graph.v1.(*manualInlineSave).value
				pointed := graph.v2.(*system).v1.(*countedSave)
				if got, want := pointed.value, int64(17); got != want {
					t.Errorf("pointed value = %d, want %d", got, want)
				}
				if got, want := inline.value, pointed.value; got != want {
					t.Errorf("inline value = %d, want %d", got, want)
				}
			},
		},
		{
			name: "pointer_before_inline",
			makeGraph: func(calls *int) *system {
				child := &countedSave{value: 17, calls: calls}
				return &system{v1: child, v2: &manualInlineSave{value: child}}
			},
			check: func(t *testing.T, graph *system) {
				inline := graph.v2.(*manualInlineSave).value
				pointed := graph.v1.(*countedSave)
				if got, want := pointed.value, int64(17); got != want {
					t.Errorf("pointed value = %d, want %d", got, want)
				}
				if got, want := inline.value, pointed.value; got != want {
					t.Errorf("inline value = %d, want %d", got, want)
				}
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			var calls int
			graph := test.makeGraph(&calls)
			var encoded bytes.Buffer
			if _, err := state.Save(t.Context(), &encoded, &graph); err != nil {
				t.Fatal(err)
			}
			if got, want := calls, 1; got != want {
				t.Fatalf("save calls = %d, want %d", got, want)
			}
			var loaded *system
			if _, err := state.Load(t.Context(), &encoded, &loaded); err != nil {
				t.Fatal(err)
			}
			test.check(t, loaded)
		})
	}
}

func TestZeroStructSnapshotOwnership(t *testing.T) {
	for _, name := range []string{"shared_dummy", "late_parent"} {
		t.Run(name, func(t *testing.T) {
			var graph *system
			if name == "shared_dummy" {
				child := new(countedZeroSave)
				graph = &system{v1: child, v2: &system{v1: child}}
			} else {
				parent := &countedZeroSaveParent{value: 23}
				graph = &system{v1: &parent.child, v2: &system{v1: parent}}
			}
			var calls int
			ctx := context.WithValue(t.Context(), zeroSaveCountKey{}, &calls)
			var encoded bytes.Buffer
			if _, err := state.Save(ctx, &encoded, &graph); err != nil {
				t.Fatal(err)
			}
			if got, want := calls, 1; got != want {
				t.Fatalf("save calls = %d, want %d", got, want)
			}
			var loaded *system
			if _, err := state.Load(t.Context(), &encoded, &loaded); err != nil {
				t.Fatal(err)
			}
			var child *countedZeroSave
			if name == "shared_dummy" {
				child = loaded.v2.(*system).v1.(*countedZeroSave)
			} else {
				parent := loaded.v2.(*system).v1.(*countedZeroSaveParent)
				child = &parent.child
				if got, want := parent.value, int64(23); got != want {
					t.Errorf("parent value = %d, want %d", got, want)
				}
			}
			if got, want := loaded.v1.(*countedZeroSave), child; got != want {
				t.Errorf("zero-sized alias = %p, want %p", got, want)
			}
		})
	}
}
