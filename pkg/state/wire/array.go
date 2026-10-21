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

package wire

import "slices"

// Array is an owned homogeneous array. Capture and Load use the same typed
// payloads. Element is for inspection; primitive restoration reads the typed
// contents directly, without boxing each element. Pointer-valued elements are
// copied so retaining one element does not retain the array's backing storage.
// Load uses IntArray[Int], UintArray[Uint] and Float32Array[Float32]; capture
// may retain a different native width until writing the same wire payload.
type Array interface {
	Object
	Len() int
	Element(int) Object
	array()
}

type arrayKind struct{}

func (arrayKind) array() {}

func snapshotValues[T any](values []T) []T {
	if len(values) == 0 {
		// An empty snapshot must not retain the source's backing object.
		return nil
	}
	return slices.Clone(values)
}

func saveArrayHeader(w *Writer, length int, kind Uint) bool {
	Uint(length).save(w)
	if length == 0 {
		return false
	}
	kind.save(w)
	return true
}

// CaptureBoolArray copies values now and emits the existing Array wire form.
func CaptureBoolArray(values []bool) Object {
	return &BoolArray{Contents: snapshotValues(values)}
}

// BoolArray owns homogeneous boolean values.
type BoolArray struct {
	arrayKind
	Contents []bool
}

func (a *BoolArray) save(w *Writer) {
	if !saveArrayHeader(w, len(a.Contents), typeBool) {
		return
	}
	for _, value := range a.Contents {
		Bool(value).save(w)
	}
}

// CaptureIntArray copies values now and emits the existing Array wire form.
func CaptureIntArray[T ~int | ~int8 | ~int16 | ~int32 | ~int64](values []T) Object {
	return &IntArray[T]{Contents: snapshotValues(values)}
}

// IntArray owns homogeneous signed integer values.
type IntArray[T ~int | ~int8 | ~int16 | ~int32 | ~int64] struct {
	arrayKind
	Contents []T
}

func (a *IntArray[T]) save(w *Writer) {
	if !saveArrayHeader(w, len(a.Contents), typeInt) {
		return
	}
	for _, value := range a.Contents {
		Int(value).save(w)
	}
}

// CaptureUintArray copies values now and emits the existing Array wire form.
func CaptureUintArray[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr](values []T) Object {
	return &UintArray[T]{Contents: snapshotValues(values)}
}

// UintArray owns homogeneous unsigned integer values.
type UintArray[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr] struct {
	arrayKind
	Contents []T
}

func (a *UintArray[T]) save(w *Writer) {
	if !saveArrayHeader(w, len(a.Contents), typeUint) {
		return
	}
	for _, value := range a.Contents {
		Uint(value).save(w)
	}
}

// CaptureFloat32Array copies values now and emits the existing Array wire form.
func CaptureFloat32Array(values []float32) Object {
	// The existing encoder promotes through reflect.Value.Float before
	// narrowing to wire.Float32. Preserve that conversion at capture time:
	// a signaling NaN can become quiet during the promotion.
	captured := make([]float64, len(values))
	for i, value := range values {
		captured[i] = float64(value)
	}
	return &Float32Array[float64]{Contents: captured}
}

// Float32Array owns homogeneous 32-bit floating point values.
type Float32Array[T ~float32 | ~float64] struct {
	arrayKind
	Contents []T
}

func (a *Float32Array[T]) save(w *Writer) {
	if !saveArrayHeader(w, len(a.Contents), typeFloat32) {
		return
	}
	for _, value := range a.Contents {
		Float32(value).save(w)
	}
}

// CaptureFloat64Array copies values now and emits the existing Array wire form.
func CaptureFloat64Array(values []float64) Object {
	return &Float64Array{Contents: snapshotValues(values)}
}

// Float64Array owns homogeneous 64-bit floating point values.
type Float64Array struct {
	arrayKind
	Contents []float64
}

func (a *Float64Array) save(w *Writer) {
	if !saveArrayHeader(w, len(a.Contents), typeFloat64) {
		return
	}
	for _, value := range a.Contents {
		Float64(value).save(w)
	}
}

// CaptureComplex64Array copies values now and emits the existing Array wire form.
func CaptureComplex64Array(values []complex64) Object {
	captured := make([]Complex64, len(values))
	for i, value := range values {
		captured[i] = Complex64(complex128(value))
	}
	return &Complex64Array{Contents: captured}
}

// Complex64Array owns homogeneous 64-bit complex values.
type Complex64Array struct {
	arrayKind
	Contents []Complex64
}

func (a *Complex64Array) save(w *Writer) {
	if !saveArrayHeader(w, len(a.Contents), typeComplex64) {
		return
	}
	for _, value := range a.Contents {
		v := Complex64(value)
		v.save(w)
	}
}

// CaptureComplex128Array copies values now and emits the existing Array wire form.
func CaptureComplex128Array(values []complex128) Object {
	captured := make([]Complex128, len(values))
	for i, value := range values {
		captured[i] = Complex128(value)
	}
	return &Complex128Array{Contents: captured}
}

// Complex128Array owns homogeneous 128-bit complex values.
type Complex128Array struct {
	arrayKind
	Contents []Complex128
}

func (a *Complex128Array) save(w *Writer) {
	if !saveArrayHeader(w, len(a.Contents), typeComplex128) {
		return
	}
	for _, value := range a.Contents {
		v := Complex128(value)
		v.save(w)
	}
}

// CaptureStringArray copies values now and emits the existing Array wire form.
func CaptureStringArray(values []string) Object {
	return &StringArray{Contents: snapshotValues(values)}
}

// StringArray owns homogeneous string values.
type StringArray struct {
	arrayKind
	Contents []string
}

func (a *StringArray) save(w *Writer) {
	if !saveArrayHeader(w, len(a.Contents), typeString) {
		return
	}
	for _, value := range a.Contents {
		v := String(value)
		v.save(w)
	}
}

// RefArray stores a homogeneous reference array without one Object interface
// and allocation per element. Contents must not move while a graph resolver
// retains element pointers. Each save reads the current reference values.
// Load uses this same storage for the ordinary Array wire grammar.
type RefArray struct {
	arrayKind
	Contents []Ref
}

func (a *RefArray) save(w *Writer) {
	if !saveArrayHeader(w, len(a.Contents), typeRef) {
		return
	}
	for i := range a.Contents {
		a.Contents[i].save(w)
	}
}

func (a *BoolArray) Len() int                   { return len(a.Contents) }
func (a *BoolArray) Element(i int) Object       { return Bool(a.Contents[i]) }
func (a *IntArray[T]) Len() int                 { return len(a.Contents) }
func (a *IntArray[T]) Element(i int) Object     { return Int(a.Contents[i]) }
func (a *UintArray[T]) Len() int                { return len(a.Contents) }
func (a *UintArray[T]) Element(i int) Object    { return Uint(a.Contents[i]) }
func (a *Float32Array[T]) Len() int             { return len(a.Contents) }
func (a *Float32Array[T]) Element(i int) Object { return Float32(a.Contents[i]) }
func (a *Float64Array) Len() int                { return len(a.Contents) }
func (a *Float64Array) Element(i int) Object    { return Float64(a.Contents[i]) }
func (a *Complex64Array) Len() int              { return len(a.Contents) }
func (a *Complex64Array) Element(i int) Object {
	value := a.Contents[i]
	return &value
}
func (a *Complex128Array) Len() int { return len(a.Contents) }
func (a *Complex128Array) Element(i int) Object {
	value := a.Contents[i]
	return &value
}
func (a *StringArray) Len() int { return len(a.Contents) }
func (a *StringArray) Element(i int) Object {
	value := String(a.Contents[i])
	return &value
}
func (a *RefArray) Len() int { return len(a.Contents) }
func (a *RefArray) Element(i int) Object {
	value := a.Contents[i]
	return &value
}
