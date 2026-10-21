// Copyright 2020 The gVisor Authors.
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

package state

import (
	"fmt"
	"reflect"
	"unsafe"

	"gvisor.dev/gvisor/pkg/state/wire"
)

// reflectValueRWAddr is equivalent to obj.Addr(), except that the returned
// reflect.Value is usable in assignments even if obj was obtained by the use
// of unexported struct fields.
//
// Preconditions: obj.CanAddr().
func reflectValueRWAddr(obj reflect.Value) reflect.Value {
	return reflect.NewAt(obj.Type(), unsafe.Pointer(obj.UnsafeAddr()))
}

// reflectValueRWSlice3 is equivalent to arr.Slice3(i, j, k), except that the
// returned reflect.Value is usable in assignments even if obj was obtained by
// the use of unexported struct fields.
//
// Preconditions:
//   - arr.Kind() == reflect.Array and arr.CanAddr().
//   - i, j, k >= 0.
//   - i <= j <= k <= arr.Len().
func reflectValueRWSlice3(arr reflect.Value, i, j, k int) reflect.Value {
	if arr.Kind() != reflect.Array {
		panic(fmt.Sprintf("arr has kind %v, wanted %v", arr.Kind(), reflect.Array))
	}
	return reflectValueRWAddr(arr).Elem().Slice3(i, j, k)
}

// These bulk operations are selected by the destination element type. They
// use typed stores, including Go's write barriers for string elements.
func boolArray[T ~bool](base unsafe.Pointer, length int, encoded wire.Array) {
	target := unsafe.Slice((*T)(base), length)
	for i, value := range requireWire[*wire.BoolArray](encoded).Contents {
		target[i] = T(value)
	}
}

func intArray[T ~int | ~int8 | ~int16 | ~int32 | ~int64](base unsafe.Pointer, length int, encoded wire.Array) {
	restoreIntArray(unsafe.Slice((*T)(base), length), requireWire[*wire.IntArray[wire.Int]](encoded).Contents)
}

func uintArray[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr](base unsafe.Pointer, length int, encoded wire.Array) {
	restoreUintArray(unsafe.Slice((*T)(base), length), requireWire[*wire.UintArray[wire.Uint]](encoded).Contents)
}

func floatArray[T ~float32 | ~float64](base unsafe.Pointer, length int, encoded wire.Array) {
	target := unsafe.Slice((*T)(base), length)
	if values, ok := encoded.(*wire.Float32Array[wire.Float32]); ok {
		restoreFloatArray(target, values.Contents, false)
		return
	}
	restoreFloatArray(target, requireWire[*wire.Float64Array](encoded).Contents, true)
}

func complexArray[T ~complex64 | ~complex128](base unsafe.Pointer, length int, encoded wire.Array) {
	target := unsafe.Slice((*T)(base), length)
	if values, ok := encoded.(*wire.Complex64Array); ok {
		restoreComplexArray(target, values.Contents, false)
		return
	}
	restoreComplexArray(target, requireWire[*wire.Complex128Array](encoded).Contents, true)
}

func stringArray[T ~string](base unsafe.Pointer, length int, encoded wire.Array) {
	target := unsafe.Slice((*T)(base), length)
	for i, value := range requireWire[*wire.StringArray](encoded).Contents {
		target[i] = T(value)
	}
}

// Narrowing stores precede validation, matching reflect.SetInt/SetUint and
// the existing scalar decoder's partial-write behavior on a malformed value.
func restoreIntArray[T ~int | ~int8 | ~int16 | ~int32 | ~int64](target []T, values []wire.Int) {
	for i, value := range values {
		target[i] = T(value)
		checkInt(int64(value), int64(target[i]))
	}
}

func restoreUintArray[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr](target []T, values []wire.Uint) {
	for i, value := range values {
		target[i] = T(value)
		checkUint(uint64(value), uint64(target[i]))
	}
}

func restoreFloatArray[T, S ~float32 | ~float64](target []T, values []S, check bool) {
	for i, value := range values {
		// Float32 payloads passed through float64 before SetFloat, including
		// its signaling-NaN conversion. Preserve that promotion here.
		promoted := float64(value)
		target[i] = T(promoted)
		if check {
			checkFloat(promoted, float64(target[i]))
		}
	}
}

func restoreComplexArray[T ~complex64 | ~complex128, S ~complex128](target []T, values []S, check bool) {
	for i, value := range values {
		target[i] = T(value)
		if check {
			checkComplex(complex128(value), complex128(target[i]))
		}
	}
}
