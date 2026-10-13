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
	"context"
	"reflect"

	"gvisor.dev/gvisor/pkg/state/wire"
)

// Decoder is a copyable view of the graph and the object currently being
// restored. Unlike Source, it does not select a struct schema or field set.
type Decoder struct {
	ds  *decodeState
	ods *objectDecodeState
}

// Allocate creates zero storage for an exact graph object type. Generated
// allocators return new(T); the dynamic allocator handles runtime shapes.
type Allocate func(reflect.Type) any

// Restore fills the authoritative *T returned by its paired Allocate.
// The graph publishes that pointer before invoking Restore. The pair must
// agree on the exact T; neither function may replace the allocation.
type Restore func(Decoder, any, wire.Value)

// NewObject allocates the exact T used by its generated restore function.
// The type argument is checked by the generator before it selects this pair.
func NewObject[T any](_ reflect.Type) any {
	return new(T)
}

func newDynamicObject(typ reflect.Type) any {
	return reflect.New(typ).Interface()
}

func restoreDynamicObject(d Decoder, pointer any, value wire.Value) {
	d.ds.decodeObject(d.ods, reflect.ValueOf(pointer).Elem(), value)
}

func registeredRestore(typ reflect.Type) (Allocate, Restore) {
	if registered, ok := restoreType(typ); ok && registered.allocate != nil {
		return registered.allocate, registered.restore
	}
	if typ.Kind() == reflect.Array {
		element := typ.Elem()
		if registered, ok := restoreType(element); ok && registered.restoreArray != nil {
			return newDynamicObject, registered.restoreArray
		}
		// A defined pointer type is not interchangeable with the unnamed
		// *T used by the registered pointer-array operation.
		if element.Kind() == reflect.Pointer && element == reflect.PointerTo(element.Elem()) {
			if registered, ok := restoreType(element.Elem()); ok && registered.restorePointerArray != nil {
				return newDynamicObject, registered.restorePointerArray
			}
		}
	}
	return newDynamicObject, restoreDynamicObject
}

func restoreType(typ reflect.Type) (registeredType, bool) {
	if info, ok := reverseTypeDatabase[typ]; ok {
		if registered, ok := globalTypeDatabase[info.Name]; ok && registered.typ == typ {
			return registered, true
		}
	}
	if registered, ok := primitiveTypeDatabase[typ.Name()]; ok && registered.typ == typ {
		return registered, true
	}
	return registeredType{}, false
}

// Context returns the context supplied to Load.
func (d Decoder) Context() context.Context {
	return d.ds.ctx
}

// Wait registers the dependencies of a value after it has been restored.
func (d Decoder) Wait(value wire.Value) {
	d.ds.waitObject(d.ods, value, nil)
}

// Decoder returns the graph context shared by this Source's fields.
func (s *Source) Decoder() Decoder {
	return Decoder{ds: s.internal.ds, ods: s.internal.ods}
}

// Field selects a local schema slot. The parsed result can be read repeatedly,
// including after the reader advances, and has already been fully validated.
func (s *Source) Field(slot int) wire.Value {
	return s.internal.field(slot)
}

// dataValue applies the wire's nil-interface delegation once at each restore
// entry. Wait registration still receives the original immutable field value.
func dataValue(value wire.Value) wire.Value {
	for value.Kind() == wire.KindInterface {
		typ, inner := value.Interface()
		if _, ok := typ.(wire.TypeSpecNil); !ok {
			break
		}
		value = inner
	}
	return value
}

// restoreEnvelope handles shared no-op, schema and dynamic-interface
// operations after a typed destination has handled its other supported forms.
// Unsupported kinds do not re-enter a second value decoder.
func restoreEnvelope(d Decoder, target any, value wire.Value) {
	switch value.Kind() {
	case wire.KindNil:
		return
	case wire.KindRef:
		if objectID(value.Reference().Root) == 0 {
			return
		}
	case wire.KindSlice:
		if objectID(value.Slice().Ref.Root) == 0 {
			return
		}
	case wire.KindInterface:
		d.ds.decodeInterface(d.ods, reflect.ValueOf(target).Elem(), value)
		return
	case wire.KindStruct:
		// The legacy schema operation can name a scalar too. It reconciles
		// the schema and invokes a receiver only if one actually exists.
		d.ds.decodeStruct(d.ods, reflect.ValueOf(target).Elem(), value)
		return
	}
	Failf("wire kind %d cannot restore %v", value.Kind(), reflect.TypeOf(target).Elem())
}

// RestoreBool restores a compiled boolean destination.
func RestoreBool[T ~bool](d Decoder, target *T, value wire.Value) {
	value = dataValue(value)
	if value.Kind() == wire.KindBool {
		*target = T(value.Bool())
		return
	}
	restoreEnvelope(d, target, value)
}

// RestoreInt restores a compiled signed destination, checking after the store
// as the ordinary decoder does.
func RestoreInt[T ~int | ~int8 | ~int16 | ~int32 | ~int64](d Decoder, target *T, value wire.Value) {
	value = dataValue(value)
	if value.Kind() == wire.KindInt {
		x := value.Int()
		*target = T(x)
		checkInt(x, int64(*target))
		return
	}
	restoreEnvelope(d, target, value)
}

// RestoreUint restores a compiled unsigned destination.
func RestoreUint[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr](d Decoder, target *T, value wire.Value) {
	value = dataValue(value)
	if value.Kind() == wire.KindUint {
		x := value.Uint()
		*target = T(x)
		checkUint(x, uint64(*target))
		return
	}
	restoreEnvelope(d, target, value)
}

// RestoreFloat restores either wire float width into a compiled float field.
func RestoreFloat[T ~float32 | ~float64](d Decoder, target *T, value wire.Value) {
	value = dataValue(value)
	switch value.Kind() {
	case wire.KindFloat32:
		*target = T(value.Float())
	case wire.KindFloat64:
		x := value.Float()
		*target = T(x)
		checkFloat(x, float64(*target))
	default:
		restoreEnvelope(d, target, value)
	}
}

// RestoreComplex restores either wire complex width into a compiled field.
func RestoreComplex[T ~complex64 | ~complex128](d Decoder, target *T, value wire.Value) {
	value = dataValue(value)
	switch value.Kind() {
	case wire.KindComplex64:
		*target = T(value.Complex())
	case wire.KindComplex128:
		x := value.Complex()
		*target = T(x)
		checkComplex(x, complex128(*target))
	default:
		restoreEnvelope(d, target, value)
	}
}

// RestoreString restores a compiled string destination. Its legacy Array form
// uses the common array operation, retaining read-only string element views.
func RestoreString[T ~string](d Decoder, target *T, value wire.Value) {
	value = dataValue(value)
	switch value.Kind() {
	case wire.KindString:
		*target = T(value.Text())
	case wire.KindArray:
		d.ds.decodeArray(d.ods, reflect.ValueOf(target).Elem(), value)
	default:
		restoreEnvelope(d, target, value)
	}
}

// RestoreStruct invokes a compiler-known struct receiver after the common
// schema and completion preparation. T must be a struct; the generator checks
// this separately from its SaverLoader method set.
func RestoreStruct[T any, P interface {
	*T
	SaverLoader
}](d Decoder, target *T, value wire.Value) {
	restoreStruct(d, P(target), value)
}

// restoreStruct shares schema preparation, accounting and receiver dispatch
// across generated pointer types. target is always a typed pointer, including
// when its value is nil. Pointer metadata is read after selecting a Struct value.
func restoreStruct(d Decoder, target SaverLoader, value wire.Value) {
	value = dataValue(value)
	if value.Kind() != wire.KindStruct {
		restoreEnvelope(d, target, value)
		return
	}
	pointer := reflect.ValueOf(target)
	if pointer.IsNil() {
		Failf("cannot restore a struct through nil %T", target)
	}
	source, ok := d.ds.structSource(d.ods, pointer.Type().Elem(), value)
	if !ok {
		return
	}
	defer d.ds.stats.done()
	target.StateLoad(d.ds.ctx, source)
}

// RestoreOpaque adapts a genuinely runtime or inaccessible destination type
// to the same value operations. Generated known fields use their typed stores.
func RestoreOpaque[T any](d Decoder, target *T, value wire.Value) {
	d.ds.decodeObject(d.ods, reflect.ValueOf(target).Elem(), value)
}

// RestorePointer restores a compiled pointer slot. Its optional static pair
// owns a new no-Dots pointee, never an existing or enclosing graph allocation.
func RestorePointer[P ~*T, T any](d Decoder, target *P, value wire.Value, restore Restore) {
	value = dataValue(value)
	switch value.Kind() {
	case wire.KindRef:
		ref := value.Reference()
		if objectID(ref.Root) == 0 {
			return
		}
		var allocate Allocate
		if restore != nil {
			allocate = NewObject[T]
		}
		pointer := d.ds.register(&ref, reflect.TypeFor[T](), allocate, restore)
		if len(ref.Dots) != 0 {
			child := walkChild(ref.Dots, reflect.ValueOf(pointer).Elem())
			pointer = reflectValueRWAddr(child).Interface()
		}
		pointee := pointer.(*T)
		if pointee == nil {
			// A nil caller-supplied root has no addressable referent.
			Failf("reference %d has no object storage", ref.Root)
		}
		*target = P(pointee)
	case wire.KindSlice:
		d.ds.assignSlice(reflect.ValueOf(target).Elem(), value.Slice())
	case wire.KindArray:
		d.ds.decodeArray(d.ods, reflect.ValueOf(target).Elem(), value)
	case wire.KindMap:
		d.ds.decodeMap(d.ods, reflect.ValueOf(target).Elem(), value)
	default:
		restoreEnvelope(d, target, value)
	}
}

// RestoreSlice restores a compiled slice header. Its backing allocation has a
// runtime extent and retains the graph's existing array and slicing adapters.
func RestoreSlice[S ~[]T, T any](d Decoder, target *S, value wire.Value, element func(Decoder, *T, wire.Value)) {
	value = dataValue(value)
	switch value.Kind() {
	case wire.KindSlice:
		d.ds.assignSlice(reflect.ValueOf(target).Elem(), value.Slice())
	case wire.KindArray:
		restoreElements[*T](d, typedElements[T]{values: *target, decode: element}, value)
	case wire.KindRef:
		d.ds.assignReference(reflect.ValueOf(target).Elem(), value.Reference())
	case wire.KindMap:
		d.ds.decodeMap(d.ods, reflect.ValueOf(target).Elem(), value)
	default:
		restoreEnvelope(d, target, value)
	}
}

func checkInt(value, stored int64) {
	if stored != value {
		Failf("signed integer truncated from %v to %v", value, stored)
	}
}

func checkUint(value, stored uint64) {
	if stored != value {
		Failf("unsigned integer truncated from %v to %v", value, stored)
	}
}

func checkFloat(value, stored float64) {
	if !isFloatEq(stored, value) {
		Failf("floating point number truncated from %v to %v", value, stored)
	}
}

func checkComplex(value, stored complex128) {
	if !isComplexEq(stored, value) {
		Failf("complex number truncated from %v to %v", value, stored)
	}
}
