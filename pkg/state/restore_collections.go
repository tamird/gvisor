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

// restoreElements owns array length checks, traversal and element waits for
// both typed elements and dynamically obtained (possibly read-only) views.
func restoreElements[E any, S interface {
	length() int
	element(int) E
	restore(Decoder, E, wire.Value)
}](d Decoder, storage S, value wire.Value) {
	if value.Len() != storage.length() {
		Failf("mismatching array length expect=%d, actual=%d", storage.length(), value.Len())
	}
	for i := 0; i < value.Len(); i++ {
		element := value.Element(i)
		storage.restore(d, storage.element(i), element)
		d.Wait(element)
	}
}

type reflectedElements struct {
	object reflect.Value
}

func (s reflectedElements) length() int {
	return s.object.Len()
}

func (s reflectedElements) element(i int) reflect.Value {
	return s.object.Index(i)
}

func (reflectedElements) restore(d Decoder, target reflect.Value, value wire.Value) {
	d.ds.decodeObject(d.ods, target, value)
}

type typedElements[T any] struct {
	values []T
	decode func(Decoder, *T, wire.Value)
}

func (s typedElements[T]) length() int {
	return len(s.values)
}

func (s typedElements[T]) element(i int) *T {
	return &s.values[i]
}

func (s typedElements[T]) restore(d Decoder, target *T, value wire.Value) {
	s.decode(d, target, value)
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
	switch value.Kind() {
	case wire.KindArray:
		restoreElements[*T](d, typedElements[T]{values: elements, decode: element}, value)
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
