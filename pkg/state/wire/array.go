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

// primitiveArraySnapshot shares the existing homogeneous Array encoding. It
// owns captured values, not references to an application's mutable array.
// The ordinary reader continues to return Array and its public Contents slice.
type primitiveArraySnapshot interface {
	Object
	primitiveArraySnapshot()
}

type arraySnapshot struct{}

func (arraySnapshot) primitiveArraySnapshot() {}

func (arraySnapshot) load(r *Reader) Object {
	a := loadArray(r)
	return &a
}

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
	return &boolArraySnapshot{values: snapshotValues(values)}
}

type boolArraySnapshot struct {
	arraySnapshot
	values []bool
}

func (a *boolArraySnapshot) save(w *Writer) {
	if !saveArrayHeader(w, len(a.values), typeBool) {
		return
	}
	for _, value := range a.values {
		Bool(value).save(w)
	}
}

// CaptureIntArray copies values now and emits the existing Array wire form.
func CaptureIntArray[T ~int | ~int8 | ~int16 | ~int32 | ~int64](values []T) Object {
	return &intArraySnapshot[T]{values: snapshotValues(values)}
}

type intArraySnapshot[T ~int | ~int8 | ~int16 | ~int32 | ~int64] struct {
	arraySnapshot
	values []T
}

func (a *intArraySnapshot[T]) save(w *Writer) {
	if !saveArrayHeader(w, len(a.values), typeInt) {
		return
	}
	for _, value := range a.values {
		Int(value).save(w)
	}
}

// CaptureUintArray copies values now and emits the existing Array wire form.
func CaptureUintArray[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr](values []T) Object {
	return &uintArraySnapshot[T]{values: snapshotValues(values)}
}

type uintArraySnapshot[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr] struct {
	arraySnapshot
	values []T
}

func (a *uintArraySnapshot[T]) save(w *Writer) {
	if !saveArrayHeader(w, len(a.values), typeUint) {
		return
	}
	for _, value := range a.values {
		Uint(value).save(w)
	}
}

// CaptureFloat32Array copies values now and emits the existing Array wire form.
func CaptureFloat32Array(values []float32) Object {
	return &float32ArraySnapshot{values: snapshotValues(values)}
}

type float32ArraySnapshot struct {
	arraySnapshot
	values []float32
}

func (a *float32ArraySnapshot) save(w *Writer) {
	if !saveArrayHeader(w, len(a.values), typeFloat32) {
		return
	}
	for _, value := range a.values {
		Float32(value).save(w)
	}
}

// CaptureFloat64Array copies values now and emits the existing Array wire form.
func CaptureFloat64Array(values []float64) Object {
	return &float64ArraySnapshot{values: snapshotValues(values)}
}

type float64ArraySnapshot struct {
	arraySnapshot
	values []float64
}

func (a *float64ArraySnapshot) save(w *Writer) {
	if !saveArrayHeader(w, len(a.values), typeFloat64) {
		return
	}
	for _, value := range a.values {
		Float64(value).save(w)
	}
}

// CaptureComplex64Array copies values now and emits the existing Array wire form.
func CaptureComplex64Array(values []complex64) Object {
	return &complex64ArraySnapshot{values: snapshotValues(values)}
}

type complex64ArraySnapshot struct {
	arraySnapshot
	values []complex64
}

func (a *complex64ArraySnapshot) save(w *Writer) {
	if !saveArrayHeader(w, len(a.values), typeComplex64) {
		return
	}
	for _, value := range a.values {
		v := Complex64(value)
		v.save(w)
	}
}

// CaptureComplex128Array copies values now and emits the existing Array wire form.
func CaptureComplex128Array(values []complex128) Object {
	return &complex128ArraySnapshot{values: snapshotValues(values)}
}

type complex128ArraySnapshot struct {
	arraySnapshot
	values []complex128
}

func (a *complex128ArraySnapshot) save(w *Writer) {
	if !saveArrayHeader(w, len(a.values), typeComplex128) {
		return
	}
	for _, value := range a.values {
		v := Complex128(value)
		v.save(w)
	}
}

// CaptureStringArray copies values now and emits the existing Array wire form.
func CaptureStringArray(values []string) Object {
	return &stringArraySnapshot{values: snapshotValues(values)}
}

type stringArraySnapshot struct {
	arraySnapshot
	values []string
}

func (a *stringArraySnapshot) save(w *Writer) {
	if !saveArrayHeader(w, len(a.values), typeString) {
		return
	}
	for _, value := range a.values {
		v := String(value)
		v.save(w)
	}
}
