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

// Decoder is the context for one destination operation. It does not own the
// destination or retain an operation; the graph owns all referenced storage.
type Decoder struct {
	ds      *decodeState
	ods     *objectDecodeState
	encoded wire.Object
}

func (d Decoder) child(encoded wire.Object) Decoder {
	d.encoded = encoded
	return d
}

// Destination restores a value of T. Implementations must be stateless: graph
// references may reconstruct their zero value after the discovering call ends.
type Destination[T any] interface {
	Restore(Decoder, *T)
}

// LoadField loads one field using its statically selected operation.
func LoadField[T any](s Source, slot int, target *T, op Destination[T]) {
	od := &s.internal
	restoreDestination(Decoder{ds: od.ds, ods: od.ods, encoded: od.field(slot)}, target, op)
}

// LoadWaitField is LoadField with the same dependency semantics as Source.LoadWait.
func LoadWaitField[T any](s Source, slot int, target *T, op Destination[T]) {
	od := &s.internal
	value := od.field(slot)
	restoreDestination(Decoder{ds: od.ds, ods: od.ods, encoded: value}, target, op)
	od.ds.waitObject(od.ods, value, nil)
}

func restoreDestination[T any](d Decoder, target *T, op Destination[T]) {
	d.encoded = dataValue(d.encoded)
	if _, ok := d.encoded.(wire.Nil); ok {
		return
	}
	if wrapped, ok := d.encoded.(*wire.Interface); ok {
		// Actual dynamic types retain the common allocation and assignment
		// boundary. They do not create a second typed object graph.
		d.ds.decodeInterface(d.ods, reflect.ValueOf(target).Elem(), wrapped)
		return
	}
	op.Restore(d, target)
}

// objectBody is retained only on graph objects, not on individual fields. Its
// implementations contain no destination pointer or captured state.
type objectBody interface {
	allocate(reflect.Type) reflect.Value
	restore(Decoder, reflect.Value)
}

type reflectedBody struct{}

func (reflectedBody) allocate(typ reflect.Type) reflect.Value { return reflect.New(typ).Elem() }
func (reflectedBody) restore(d Decoder, target reflect.Value) {
	d.ds.restoreValue(d.ods, target, d.encoded)
}

type typedBody[T any, O Destination[T]] struct{}

func (typedBody[T, O]) allocate(reflect.Type) reflect.Value {
	return reflect.ValueOf(new(T)).Elem()
}

func (typedBody[T, O]) restore(d Decoder, target reflect.Value) {
	var op O
	restoreDestination(d, target.Addr().Interface().(*T), op)
}

type intOps[T ~int | ~int8 | ~int16 | ~int32 | ~int64] struct{}

// IntOps selects a signed integer destination without retaining target.
func IntOps[T ~int | ~int8 | ~int16 | ~int32 | ~int64](_ *T) intOps[T] {
	return intOps[T]{}
}

func (intOps[T]) Restore(d Decoder, target *T) {
	value := requireWire[wire.Int](d.encoded)
	*target = T(value)
	checkInt(int64(value), int64(*target))
}

type boolOps[T ~bool] struct{}

// BoolOps selects a boolean destination without retaining target.
func BoolOps[T ~bool](_ *T) boolOps[T] { return boolOps[T]{} }

func (boolOps[T]) Restore(d Decoder, target *T) {
	*target = T(requireWire[wire.Bool](d.encoded))
}

type uintOps[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr] struct{}

// UintOps selects an unsigned integer destination without retaining target.
func UintOps[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr](_ *T) uintOps[T] {
	return uintOps[T]{}
}

func (uintOps[T]) Restore(d Decoder, target *T) {
	value := requireWire[wire.Uint](d.encoded)
	*target = T(value)
	checkUint(uint64(value), uint64(*target))
}

type floatOps[T ~float32 | ~float64] struct{}

// FloatOps selects a floating-point destination without retaining target.
func FloatOps[T ~float32 | ~float64](_ *T) floatOps[T] { return floatOps[T]{} }

func (floatOps[T]) Restore(d Decoder, target *T) {
	if value, ok := d.encoded.(wire.Float32); ok {
		*target = T(value)
	} else {
		value := requireWire[wire.Float64](d.encoded)
		*target = T(value)
		checkFloat(float64(value), float64(*target))
	}
}

type complexOps[T ~complex64 | ~complex128] struct{}

// ComplexOps selects a complex destination without retaining target.
func ComplexOps[T ~complex64 | ~complex128](_ *T) complexOps[T] { return complexOps[T]{} }

func (complexOps[T]) Restore(d Decoder, target *T) {
	if value, ok := d.encoded.(*wire.Complex64); ok {
		*target = T(*value)
	} else {
		value := requireWire[*wire.Complex128](d.encoded)
		*target = T(*value)
		checkComplex(complex128(*value), complex128(*target))
	}
}

type stringOps[T ~string] struct{}

// StringOps selects a string destination without retaining target.
func StringOps[T ~string](_ *T) stringOps[T] { return stringOps[T]{} }

func (stringOps[T]) Restore(d Decoder, target *T) {
	*target = T(*requireWire[*wire.String](d.encoded))
}

type valueOps[T any] struct{}

// ValueOps retains the common loader at dynamic and manual boundaries.
func ValueOps[T any](_ *T) valueOps[T] { return valueOps[T]{} }

func (valueOps[T]) Restore(d Decoder, target *T) {
	d.ds.restoreValue(d.ods, reflect.ValueOf(target).Elem(), d.encoded)
}

type structOps[T any] struct{}

// StructOps selects the shared struct loader without reflecting the receiver.
// T must be a struct; its StateLoad selects operations for its own fields.
func StructOps[T any](_ *T) structOps[T] { return structOps[T]{} }

func (structOps[T]) Restore(d Decoder, target *T) {
	typ := reflect.TypeFor[T]()
	if typ.Kind() != reflect.Struct {
		Failf("struct operation requires a struct, got %s", typ)
	}
	d.ds.decodeStruct(d.ods, typ, target, requireWire[*wire.Struct](d.encoded))
}

type typedCell[T any, O Destination[T]] struct{}

func (typedCell[T, O]) Restore(d Decoder, target *T) {
	var op O
	restoreDestination(d, target, op)
}

type reflectedCell struct{}

func (reflectedCell) Restore(d Decoder, target reflect.Value) {
	d.ds.restoreValue(d.ods, target, d.encoded)
}

// MapTypes returns zero child values for type inference, without reading m.
func MapTypes[M ~map[K]V, K comparable, V any](_ M) (key K, value V) { return }

// SliceTypes returns a zero element for type inference, without reading s.
func SliceTypes[S ~[]E, E any](_ S) (element E) { return }

// PointerTypes returns a zero pointee for inference, without dereferencing p.
func PointerTypes[P ~*E, E any](_ P) (element E) { return }

type mapOps[M ~map[K]V, K comparable, V any, KO Destination[K], VO Destination[V]] struct{}

// MapOps selects map storage and child operations without retaining arguments.
func MapOps[M ~map[K]V, K comparable, V any, KO Destination[K], VO Destination[V]](_ *M, _ KO, _ VO) mapOps[M, K, V, KO, VO] {
	return mapOps[M, K, V, KO, VO]{}
}

func (op mapOps[M, K, V, KO, VO]) Restore(d Decoder, target *M) {
	if ref, ok := d.encoded.(*wire.Ref); ok {
		if objectID(ref.Root) != 0 {
			object := walkChild(ref, d.ds.register(ref, reflect.TypeFor[M](), typedBody[M, mapOps[M, K, V, KO, VO]]{}))
			// Preserve assignability between named and unnamed maps and the
			// existing read-only flags on interior map fields.
			assignMap(reflect.ValueOf(target).Elem(), object)
		}
		return
	}
	restoreMapEntries(d, typedMap[M, K, V]{target}, typedCell[K, KO]{}, typedCell[V, VO]{}, requireWire[*wire.Map](d.encoded))
}

type typedMap[M ~map[K]V, K comparable, V any] struct{ target *M }

func (m typedMap[M, K, V]) ensure() {
	if *m.target == nil {
		*m.target = make(M)
	}
}
func (typedMap[M, K, V]) key() *K   { return new(K) }
func (typedMap[M, K, V]) value() *V { return new(V) }
func (typedMap[M, K, V]) zeroKey(p *K) {
	var zero K
	*p = zero
}
func (typedMap[M, K, V]) zeroValue(p *V) {
	var zero V
	*p = zero
}
func (typedMap[M, K, V]) reuseKey() bool   { return reusableCell(reflect.TypeFor[K]()) }
func (typedMap[M, K, V]) reuseValue() bool { return reusableCell(reflect.TypeFor[V]()) }
func (m typedMap[M, K, V]) insert(key *K, value *V) {
	// Hooks may have replaced the header; always insert through the live cell.
	(*m.target)[*key] = *value
}

type sliceOps[S ~[]E, E any, EO Destination[E]] struct{}

// SliceOps selects a slice and its element operation without retaining arguments.
func SliceOps[S ~[]E, E any, EO Destination[E]](_ *S, _ EO) sliceOps[S, E, EO] {
	return sliceOps[S, E, EO]{}
}

func (sliceOps[S, E, EO]) Restore(d Decoder, target *S) {
	if slice, ok := d.encoded.(*wire.Slice); ok {
		if objectID(slice.Ref.Root) != 0 {
			typ := reflect.ArrayOf(int(slice.Capacity), reflect.TypeFor[E]())
			object := walkChild(&slice.Ref, d.ds.register(&slice.Ref, typ, backingArrayBody[E, EO]{}))
			reflect.ValueOf(target).Elem().Set(reflectValueRWSlice3(object, 0, int(slice.Length), int(slice.Capacity)))
		}
		return
	}
	// Manual Array-to-slice loads must re-read the actual slice header after
	// each hook, rather than indexing a snapshot of it.
	restoreArrayElements(d, typedSlice[S, E]{target}, typedCell[E, EO]{}, requireWire[wire.Array](d.encoded))
}

type arrayOps[A any, E any, EO Destination[E]] struct{}

// ArrayOps selects a fixed array and its element operation. The view only
// supplies the element type; neither it nor the destination is retained.
func ArrayOps[A any, E any, EO Destination[E]](_ *A, _ []E, _ EO) arrayOps[A, E, EO] {
	return arrayOps[A, E, EO]{}
}

func (arrayOps[A, E, EO]) Restore(d Decoder, target *A) {
	// Go has no array-length type parameter. Obtain one view, then use the
	// same typed element traversal as graph-owned backing arrays.
	elements := arrayView[E](reflect.ValueOf(target).Elem())
	restoreArrayElements(d, typedArray[E]{elements}, typedCell[E, EO]{}, requireWire[wire.Array](d.encoded))
}

type backingArrayBody[E any, EO Destination[E]] struct{}

func (backingArrayBody[E, EO]) allocate(typ reflect.Type) reflect.Value {
	// The array length is part of the wire graph, not a static Go parameter.
	return reflect.New(typ).Elem()
}

func (backingArrayBody[E, EO]) restore(d Decoder, target reflect.Value) {
	value := dataValue(d.encoded)
	if _, ok := value.(wire.Nil); ok {
		return
	}
	if _, ok := value.(*wire.Interface); ok {
		d.ds.restoreValue(d.ods, target, value)
		return
	}
	// One view of the authoritative complete array, including unused slice
	// capacity. The array's allocation and interior addresses never change.
	elements := arrayView[E](target)
	restoreArrayElements(d, typedArray[E]{elements}, typedCell[E, EO]{}, requireWire[wire.Array](value))
}

// typedArray holds a fixed view of a graph allocation. Unlike a manual slice
// destination, its header cannot be replaced by a load hook.
type typedArray[E any] struct{ elements []E }

func (a typedArray[E]) length() int   { return len(a.elements) }
func (a typedArray[E]) cell(i int) *E { return &a.elements[i] }
func (a typedArray[E]) primitives(encoded wire.Array) {
	restorePrimitiveArray(reflect.ValueOf(a.elements), encoded)
}

type typedSlice[S ~[]E, E any] struct{ target *S }

func (s typedSlice[S, E]) length() int   { return len(*s.target) }
func (s typedSlice[S, E]) cell(i int) *E { return &(*s.target)[i] }
func (s typedSlice[S, E]) primitives(encoded wire.Array) {
	restorePrimitiveArray(reflect.ValueOf(*s.target), encoded)
}

type pointerOps[P ~*E, E any, EO Destination[E]] struct{}

// PointerOps selects the referenced object's operation without retaining arguments.
func PointerOps[P ~*E, E any, EO Destination[E]](_ *P, _ EO) pointerOps[P, E, EO] {
	return pointerOps[P, E, EO]{}
}

func (pointerOps[P, E, EO]) Restore(d Decoder, target *P) {
	ref := requireWire[*wire.Ref](d.encoded)
	if objectID(ref.Root) == 0 {
		return
	}
	object := d.ds.register(ref, reflect.TypeFor[E](), typedBody[E, EO]{})
	if len(ref.Dots) != 0 {
		reflect.ValueOf(target).Elem().Set(reflectValueRWAddr(walkChild(ref, object)))
	} else {
		reflect.ValueOf(target).Elem().Set(object.Addr())
	}
}
