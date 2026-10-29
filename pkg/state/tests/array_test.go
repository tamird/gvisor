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
	"reflect"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
)

var allArrayPrimitives = []any{
	[1]bool{},
	[1]bool{true},
	[2]bool{false, true},
	[1]int{},
	[1]int{1},
	[2]int{0, 1},
	[1]int8{},
	[1]int8{1},
	[2]int8{0, 1},
	[1]int16{},
	[1]int16{1},
	[2]int16{0, 1},
	[1]int32{},
	[1]int32{1},
	[2]int32{0, 1},
	[1]int64{},
	[1]int64{1},
	[2]int64{0, 1},
	[1]uint{},
	[1]uint{1},
	[2]uint{0, 1},
	[1]uintptr{},
	[1]uintptr{1},
	[2]uintptr{0, 1},
	[1]uint8{},
	[1]uint8{1},
	[2]uint8{0, 1},
	[1]uint16{},
	[1]uint16{1},
	[2]uint16{0, 1},
	[1]uint32{},
	[1]uint32{1},
	[2]uint32{0, 1},
	[1]uint64{},
	[1]uint64{1},
	[2]uint64{0, 1},
	[1]string{},
	[1]string{""},
	[1]string{nonEmptyString},
	[2]string{"", nonEmptyString},
}

func TestArrayPrimitives(t *testing.T) {
	runTestCases(t, false, "plain", flatten(allArrayPrimitives))
	runTestCases(t, false, "pointers", pointersTo(flatten(allArrayPrimitives)))
	runTestCases(t, false, "interfaces", interfacesTo(flatten(allArrayPrimitives)))
	runTestCases(t, false, "interfacesToPointers", interfacesTo(pointersTo(flatten(allArrayPrimitives))))
}

func TestSlices(t *testing.T) {
	var allSlices = flatten(
		filter(allArrayPrimitives, func(o any) (any, bool) {
			v := reflect.New(reflect.TypeOf(o)).Elem()
			v.Set(reflect.ValueOf(o))
			return v.Slice(0, v.Len()).Interface(), true
		}),
		filter(allArrayPrimitives, func(o any) (any, bool) {
			v := reflect.New(reflect.TypeOf(o)).Elem()
			v.Set(reflect.ValueOf(o))
			if v.Len() == 0 {
				// Return the pure "nil" value for the slice.
				return reflect.New(v.Slice(0, 0).Type()).Elem().Interface(), true
			}
			return v.Slice(1, v.Len()).Interface(), true
		}),
		filter(allArrayPrimitives, func(o any) (any, bool) {
			v := reflect.New(reflect.TypeOf(o)).Elem()
			v.Set(reflect.ValueOf(o))
			if v.Len() == 0 {
				// Return the zero-valued slice.
				return reflect.MakeSlice(v.Slice(0, 0).Type(), 0, 0).Interface(), true
			}
			return v.Slice(0, v.Len()-1).Interface(), true
		}),
	)
	runTestCases(t, false, "plain", allSlices)
	runTestCases(t, false, "pointers", pointersTo(allSlices))
	runTestCases(t, false, "interfaces", interfacesTo(allSlices))
	runTestCases(t, false, "interfacesToPointers", interfacesTo(pointersTo(allSlices)))
}

func TestArrayContainers(t *testing.T) {
	var (
		emptyArray [1]any
		fullArray  [1]any
	)
	fullArray[0] = &emptyArray
	runTestCases(t, false, "", []any{
		arrayContainer{v: emptyArray},
		arrayContainer{v: fullArray},
		arrayPtrContainer{v: nil},
		arrayPtrContainer{v: &emptyArray},
		arrayPtrContainer{v: &fullArray},
	})
}

func TestSliceContainers(t *testing.T) {
	var (
		nilSlice            []any
		emptySlice          = make([]any, 0)
		fullSlice           = []any{nil}
		unusedCapacitySlice = []any{savableEmptyStruct{}, unregisteredEmptyStruct{}}[:1]
	)
	runTestCases(t, false, "", []any{
		sliceContainer{v: nilSlice},
		sliceContainer{v: emptySlice},
		sliceContainer{v: fullSlice},
		sliceContainer{v: unusedCapacitySlice},
		slicePtrContainer{v: nil},
		slicePtrContainer{v: &nilSlice},
		slicePtrContainer{v: &emptySlice},
		slicePtrContainer{v: &fullSlice},
		slicePtrContainer{v: &unusedCapacitySlice},
	})
}

type blockedCheckpointWriter struct {
	buf     bytes.Buffer
	entered chan struct{}
	resume  chan struct{}
}

func (w *blockedCheckpointWriter) Write(p []byte) (int, error) {
	if w.entered != nil {
		close(w.entered)
		<-w.resume
		w.entered = nil
	}
	return w.buf.Write(p)
}

// TestSaveSliceAliases takes an independent snapshot before Save. A
// comparison against the same backing array after Save would miss corruption
// that affected both the input and restored output.
func TestSaveSliceAliases(t *testing.T) {
	state.EnableGraphEngine(true)
	t.Cleanup(func() { state.EnableGraphEngine(false) })
	for _, tc := range []struct {
		name    string
		lengths [2]int
		long    int
	}{
		{"both-long", [2]int{3, 3}, 0},
		{"short-first", [2]int{1, 3}, 1},
		{"long-first", [2]int{3, 1}, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			want := [3]byte{0x11, 0x22, 0x33}
			backing := want
			in := [2][]byte{backing[:tc.lengths[0]], backing[:tc.lengths[1]]}
			entered := make(chan struct{})
			w := blockedCheckpointWriter{entered: entered, resume: make(chan struct{})}
			done := make(chan error, 1)
			go func() {
				_, err := state.Save(t.Context(), &w, &in)
				done <- err
			}()
			select {
			case <-entered:
				if got := [3]byte(in[tc.long]); got != want {
					t.Errorf("live while Save is writing = %x, want %x", got, want)
				}
				close(w.resume)
				if err := <-done; err != nil {
					t.Fatal(err)
				}
			case err := <-done:
				t.Fatalf("Save returned before writing: %v", err)
			}
			if got := [3]byte(in[tc.long]); got != want {
				t.Errorf("live after Save = %x, want %x", got, want)
			}
			var out [2][]byte
			if _, err := state.Load(t.Context(), &w.buf, &out); err != nil {
				t.Fatal(err)
			}
			for i, s := range out {
				if got, want := [2]int{len(s), cap(s)}, [2]int{tc.lengths[i], len(backing)}; got != want {
					t.Fatalf("restored view %d dimensions = %v, want %v", i, got, want)
				}
			}
			if got := [3]byte(out[tc.long]); got != want {
				t.Errorf("restored longer view = %x, want %x", got, want)
			}
			if &out[0][0] != &out[1][0] {
				t.Errorf("restored slices have distinct backing arrays")
			}
		})
	}
}
