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
	"fmt"
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

func TestSliceInteriorField(t *testing.T) {
	state.EnableGraphEngine(true)
	t.Cleanup(func(){state.EnableGraphEngine(false)})
	for _, fieldFirst := range []bool{false, true} {
		t.Run(fmt.Sprintf("fieldFirst=%v", fieldFirst), func(t *testing.T) {
			secret := "hidden-slice-referent:5eb351"
			cells := [1]system{{v1: int64(17), v2: &secret}}
			const want = int64(17)
			shortIndex := 0
			input := [2]any{cells[:0], &cells[0].v1}
			if fieldFirst {
				input[0], input[1] = input[1], input[0]
				shortIndex = 1
			}
			var b bytes.Buffer
			if _, err := state.Save(t.Context(), &b, &input); err != nil {
				t.Fatal(err)
			}
			if got := cells[0].v1; got != want {
				t.Errorf("Save changed live First to %v", got)
			}
			if got := cells[0].v2; got != &secret {
				t.Errorf("Save changed live Other to %v", got)
			}
			if bytes.Contains(b.Bytes(), []byte(secret)) {
				t.Error("wire contains hidden Other marker")
			}
			var out [2]any
			if _, err := state.Load(t.Context(), &b, &out); err != nil {
				t.Fatal(err)
			}
			short := out[shortIndex].([]system)
			if got, want := [2]int{len(short), cap(short)}, [2]int{0, 1}; got != want {
				t.Fatalf("restored slice dimensions %v, want %v", got, want)
			}
			cell := &short[:1][0]
			ref := out[1-shortIndex].(*any)
			if ref != &cell.v1 {
				t.Error("restored First pointer does not alias the original field")
			}
			if got := *ref; got != want {
				t.Errorf("restored First = %v, want %v", got, want)
			}
			if cell.v2 != nil {
				t.Errorf("restored Other is nonnil: %T", cell.v2)
			}
		})
	}
}
