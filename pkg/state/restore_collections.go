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

package state

import (
	"reflect"

	"gvisor.dev/gvisor/pkg/state/wire"
)

type elements interface {
	length() int
	restoreElement(Decoder, int, wire.Value)
}

// restoreElements owns array length checks, traversal and element waits for
// both typed elements and dynamically obtained (possibly read-only) views.
func restoreElements(d Decoder, storage elements, value wire.Value) {
	if value.Len() != storage.length() {
		Failf("mismatching array length expect=%d, actual=%d", storage.length(), value.Len())
	}
	for i := 0; i < value.Len(); i++ {
		element := value.Element(i)
		storage.restoreElement(d, i, element)
		d.Wait(element)
	}
}

type reflectedElements struct {
	object reflect.Value
}

func (s reflectedElements) length() int {
	return s.object.Len()
}

func (s reflectedElements) restoreElement(d Decoder, i int, value wire.Value) {
	d.ds.decodeObject(d.ods, s.object.Index(i), value)
}

type typedElements[T any] struct {
	values []T
	decode func(Decoder, *T, wire.Value)
}

func (s typedElements[T]) length() int {
	return len(s.values)
}

func (s typedElements[T]) restoreElement(d Decoder, i int, value wire.Value) {
	s.decode(d, &s.values[i], value)
}

type typedPointerElements[T any] struct {
	values        []*T
	restoreObject Restore
}

func (s typedPointerElements[T]) length() int {
	return len(s.values)
}

func (s typedPointerElements[T]) restoreElement(d Decoder, i int, value wire.Value) {
	RestorePointer(d, &s.values[i], value, s.restoreObject)
}

// restoreEntries owns the map lifetime and ordering contract. Both cells must
// exist before either hook, waits follow both restores, and insertion reads
// the live map header. A storage adapter must not reuse nonzero-sized cells.
func restoreEntries[K, V any, S interface {
	initialize()
	cells() (K, V)
	restoreKey(Decoder, K, wire.Value)
	restoreValue(Decoder, V, wire.Value)
	insert(K, V)
}](d Decoder, storage S, value wire.Value) {
	storage.initialize()
	for i := 0; i < value.Len(); i++ {
		key, element := value.Entry(i)
		keyCell, valueCell := storage.cells()
		storage.restoreKey(d, keyCell, key)
		storage.restoreValue(d, valueCell, element)
		d.Wait(key)
		d.Wait(element)
		storage.insert(keyCell, valueCell)
	}
}

type reflectedMap struct {
	object reflect.Value
}

func (m reflectedMap) initialize() {
	if m.object.IsNil() {
		m.object.Set(reflect.MakeMap(m.object.Type()))
	}
}

func (m reflectedMap) cells() (reflect.Value, reflect.Value) {
	key := reflect.New(m.object.Type().Key()).Elem()
	value := reflect.New(m.object.Type().Elem()).Elem()
	return key, value
}

func (reflectedMap) restoreKey(d Decoder, target reflect.Value, value wire.Value) {
	d.ds.decodeObject(d.ods, target, value)
}

func (reflectedMap) restoreValue(d Decoder, target reflect.Value, value wire.Value) {
	d.ds.decodeObject(d.ods, target, value)
}

func (m reflectedMap) insert(key, value reflect.Value) {
	m.object.SetMapIndex(key, value)
}

type typedMap[M ~map[K]V, K comparable, V any] struct {
	object      *M
	decodeKey   func(Decoder, *K, wire.Value)
	decodeValue func(Decoder, *V, wire.Value)
}

func (m typedMap[M, K, V]) initialize() {
	if *m.object == nil {
		*m.object = make(M)
	}
}

func (typedMap[M, K, V]) cells() (*K, *V) {
	return new(K), new(V)
}

func (m typedMap[M, K, V]) restoreKey(d Decoder, target *K, value wire.Value) {
	m.decodeKey(d, target, value)
}

func (m typedMap[M, K, V]) restoreValue(d Decoder, target *V, value wire.Value) {
	m.decodeValue(d, target, value)
}

func (m typedMap[M, K, V]) insert(key *K, value *V) {
	(*m.object)[*key] = *value
}

// RestoreMap supplies typed cells and insertion to the shared map operation.
// restoreObject is the static callback for this exact M when a reference first
// creates a graph node; key and element operate on each fresh entry's cells.
func RestoreMap[M ~map[K]V, K comparable, V any](d Decoder, target *M, value wire.Value, restoreObject Restore, key func(Decoder, *K, wire.Value), element func(Decoder, *V, wire.Value)) {
	value = dataValue(value)
	switch value.Kind() {
	case wire.KindMap:
		restoreEntries[*K, *V](d, typedMap[M, K, V]{object: target, decodeKey: key, decodeValue: element}, value)
	case wire.KindRef:
		ref := value.Reference()
		if objectID(ref.Root) == 0 {
			return
		}
		var allocate Allocate
		if restoreObject != nil {
			allocate = NewObject[M]
		}
		pointer := d.ds.register(&ref, reflect.TypeFor[M](), allocate, restoreObject)
		if len(ref.Dots) == 0 {
			if object, ok := pointer.(*M); ok {
				typedMap[M, K, V]{object: object}.initialize()
				*target = *object
				return
			}
		}
		// Keep the actual walked type and read-only flags. Distinct
		// assignable map types still use Go's checked assignment.
		object := walkChild(ref.Dots, reflect.ValueOf(pointer).Elem())
		assignMap(reflect.ValueOf(target).Elem(), object)
	case wire.KindArray:
		d.ds.decodeArray(d.ods, reflect.ValueOf(target).Elem(), value)
	case wire.KindSlice:
		d.ds.assignSlice(reflect.ValueOf(target).Elem(), value.Slice())
	default:
		restoreEnvelope(d, target, value)
	}
}

// RestoreArray supplies typed element addresses for a compiler-known array.
// The generator must pass the array's actual pointer and its complete [:]
// view; A must be an array with element type T. This keeps legacy operations
// on the array itself distinct from operations on a slice destination.
func RestoreArray[A, T any](d Decoder, target *A, elements []T, value wire.Value, element func(Decoder, *T, wire.Value)) {
	value = dataValue(value)
	if value.Kind() == wire.KindArray {
		restoreElements(d, typedElements[T]{values: elements, decode: element}, value)
		return
	}
	restoreArrayEnvelope(d, target, value)
}

// RestoreRuntimeArray restores an array with runtime length and exact element
// type T. pointer must be the authoritative *[N]T, not an enclosing object's
// pointer. In particular, an inline interface array uses temporary storage
// whose value is copied to the interface after restoration.
func RestoreRuntimeArray[T any](d Decoder, pointer any, value wire.Value, element func(Decoder, *T, wire.Value)) {
	value = dataValue(value)
	if value.Kind() == wire.KindArray {
		values := runtimeArrayValues(pointer).([]T)
		restoreElements(d, typedElements[T]{values: values, decode: element}, value)
		return
	}
	restoreArrayEnvelope(d, pointer, value)
}

// RestoreRuntimePointerArray is RestoreRuntimeArray for exact unnamed *T
// elements. restore owns a newly encountered T, never its containing array.
func RestoreRuntimePointerArray[T any](d Decoder, pointer any, value wire.Value, restore Restore) {
	value = dataValue(value)
	if value.Kind() == wire.KindArray {
		values := runtimeArrayValues(pointer).([]*T)
		restoreElements(d, typedPointerElements[T]{values: values, restoreObject: restore}, value)
		return
	}
	restoreArrayEnvelope(d, pointer, value)
}

// runtimeArrayValues returns the complete typed slice view of the supplied
// array allocation. The interface retains the view's ordinary GC-visible data.
func runtimeArrayValues(pointer any) any {
	array := reflect.ValueOf(pointer).Elem()
	return reflectValueRWSlice3(array, 0, array.Len(), array.Len()).Interface()
}

// restoreArrayEnvelope keeps legacy operations on the array itself shared by
// compiler-known and runtime-length arrays. No-op values must not form a view.
func restoreArrayEnvelope(d Decoder, target any, value wire.Value) {
	switch value.Kind() {
	case wire.KindRef:
		d.ds.assignReference(reflect.ValueOf(target).Elem(), value.Reference())
	case wire.KindSlice:
		d.ds.assignSlice(reflect.ValueOf(target).Elem(), value.Slice())
	case wire.KindMap:
		d.ds.decodeMap(d.ods, reflect.ValueOf(target).Elem(), value)
	default:
		restoreEnvelope(d, target, value)
	}
}

// These static thunks keep the existing primitive rows typed without a second
// kind-to-type catalog or a closure allocated for each runtime array length.
func restoreBoolObject[T ~bool](d Decoder, pointer any, value wire.Value) {
	RestoreBool(d, pointer.(*T), value)
}

func restoreBoolArray[T ~bool](d Decoder, pointer any, value wire.Value) {
	RestoreRuntimeArray(d, pointer, value, RestoreBool[T])
}

func restoreBoolPointerArray[T ~bool](d Decoder, pointer any, value wire.Value) {
	RestoreRuntimePointerArray[T](d, pointer, value, restoreBoolObject[T])
}

func restoreIntObject[T ~int | ~int8 | ~int16 | ~int32 | ~int64](d Decoder, pointer any, value wire.Value) {
	RestoreInt(d, pointer.(*T), value)
}

func restoreIntArray[T ~int | ~int8 | ~int16 | ~int32 | ~int64](d Decoder, pointer any, value wire.Value) {
	RestoreRuntimeArray(d, pointer, value, RestoreInt[T])
}

func restoreIntPointerArray[T ~int | ~int8 | ~int16 | ~int32 | ~int64](d Decoder, pointer any, value wire.Value) {
	RestoreRuntimePointerArray[T](d, pointer, value, restoreIntObject[T])
}

func restoreUintObject[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr](d Decoder, pointer any, value wire.Value) {
	RestoreUint(d, pointer.(*T), value)
}

func restoreUintArray[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr](d Decoder, pointer any, value wire.Value) {
	RestoreRuntimeArray(d, pointer, value, RestoreUint[T])
}

func restoreUintPointerArray[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr](d Decoder, pointer any, value wire.Value) {
	RestoreRuntimePointerArray[T](d, pointer, value, restoreUintObject[T])
}

func restoreStringObject[T ~string](d Decoder, pointer any, value wire.Value) {
	RestoreString(d, pointer.(*T), value)
}

func restoreStringArray[T ~string](d Decoder, pointer any, value wire.Value) {
	RestoreRuntimeArray(d, pointer, value, RestoreString[T])
}

func restoreStringPointerArray[T ~string](d Decoder, pointer any, value wire.Value) {
	RestoreRuntimePointerArray[T](d, pointer, value, restoreStringObject[T])
}

func restoreFloatObject[T ~float32 | ~float64](d Decoder, pointer any, value wire.Value) {
	RestoreFloat(d, pointer.(*T), value)
}

func restoreFloatArray[T ~float32 | ~float64](d Decoder, pointer any, value wire.Value) {
	RestoreRuntimeArray(d, pointer, value, RestoreFloat[T])
}

func restoreFloatPointerArray[T ~float32 | ~float64](d Decoder, pointer any, value wire.Value) {
	RestoreRuntimePointerArray[T](d, pointer, value, restoreFloatObject[T])
}

func restoreComplexObject[T ~complex64 | ~complex128](d Decoder, pointer any, value wire.Value) {
	RestoreComplex(d, pointer.(*T), value)
}

func restoreComplexArray[T ~complex64 | ~complex128](d Decoder, pointer any, value wire.Value) {
	RestoreRuntimeArray(d, pointer, value, RestoreComplex[T])
}

func restoreComplexPointerArray[T ~complex64 | ~complex128](d Decoder, pointer any, value wire.Value) {
	RestoreRuntimePointerArray[T](d, pointer, value, restoreComplexObject[T])
}
