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
	"math"
	"reflect"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
)

var allArrayPrimitives = []any{
	[0]uint64{},
	[2]int64{-1 << 63, 1<<63 - 1},
	[2]uint64{0, ^uint64(0)},
	[2]arraySigned{-7, 11},
	[2]arrayUnsigned{0, ^arrayUnsigned(0)},
	[2]arrayString{"", "owned"},
	[2]float32{0, -1.5},
	[2]float64{math.Copysign(0, -1), math.Inf(1)},
	[2]complex64{0, 1.25 - 3i},
	[2]complex128{complex(math.Copysign(0, -1), 0), 2 - 5i},
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

func TestArraySnapshotTiming(t *testing.T) {
	forEachSave(t, func(t *testing.T, save saveFunc) {

		first := &arraySnapshotSource{values: [2]uint64{7, 11}}
		original := system{v1: first, v2: &arraySnapshotMutator{target: first}}
		var encoded bytes.Buffer
		if _, err := save(t.Context(), &encoded, &original); err != nil {
			t.Fatal(err)
		}
		if got, want := first.values[0], uint64(99); got != want {
			t.Fatalf("later hook mutation = %d, want %d", got, want)
		}
		var loaded system
		if _, err := state.Load(t.Context(), &encoded, &loaded); err != nil {
			t.Fatal(err)
		}
		child := loaded.v1.(*arraySnapshotSource)
		if got, want := child.values, ([2]uint64{7, 11}); got != want {
			t.Errorf("captured array = %v, want %v", got, want)
		}
		if got, want := loaded.v2.(*arraySnapshotMutator).target, child; got != want {
			t.Errorf("shared target = %p, want %p", got, want)
		}

	})
}

func TestArrayLateParent(t *testing.T) {
	forEachSave(t, func(t *testing.T, save saveFunc) {

		backing := [2][2]uint64{{7, 11}, {13, 17}}
		// Encode the interior array first. Later slice discovery clears the unused
		// capacity and reparents that object under the complete backing array.
		// The containing array must be captured anew after the clearing.
		original := system{v1: &backing[1], v2: &arrayTailDiscovery{values: backing[:1]}}
		var encoded bytes.Buffer
		if _, err := save(t.Context(), &encoded, &original); err != nil {
			t.Fatal(err)
		}
		if got, want := backing[1], ([2]uint64{}); got != want {
			t.Fatalf("cleared backing tail = %v, want %v", got, want)
		}
		var loaded system
		if _, err := state.Load(t.Context(), &encoded, &loaded); err != nil {
			t.Fatal(err)
		}
		child := loaded.v1.(*[2]uint64)
		parent := loaded.v2.(*arrayTailDiscovery).values
		if got, want := cap(parent), 2; got != want {
			t.Fatalf("backing capacity = %d, want %d", got, want)
		}
		if got, want := child, &parent[:2][1]; got != want {
			t.Errorf("interior array = %p, want %p", got, want)
		}
		if got, want := *child, ([2]uint64{}); got != want {
			t.Errorf("recaptured tail = %v, want %v", got, want)
		}

	})
}

func TestPrimitiveArrayFloatEncoding(t *testing.T) {
	forEachSave(t, func(t *testing.T, save saveFunc) {

		positiveSignal := math.Float32frombits(0x7f800001)
		negativeSignal := math.Float32frombits(0xff800001)
		quiet := math.Float32frombits(0x7fc00042)
		negativeZero := math.Float32frombits(0x80000000)
		for _, test := range []struct {
			name  string
			value arrayFloatEncoding
		}{
			{"float32", arrayFloatEncoding{floats: [4]float32{positiveSignal, negativeSignal, quiet, negativeZero}}},
			{"complex64", arrayFloatEncoding{complexes: [2]complex64{complex(positiveSignal, negativeSignal), complex(negativeZero, quiet)}}},
		} {
			t.Run(test.name, func(t *testing.T) {
				var captured, reflected bytes.Buffer
				if _, err := save(t.Context(), &captured, &test.value); err != nil {
					t.Fatal(err)
				}
				test.value.byValue = true
				if _, err := save(t.Context(), &reflected, &test.value); err != nil {
					t.Fatal(err)
				}
				if got, want := captured.Bytes(), reflected.Bytes(); !bytes.Equal(got, want) {
					t.Errorf("addressable array encoding = %x, want existing value encoding %x", got, want)
				}
			})
		}

	})
}
