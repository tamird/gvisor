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

// valueHandle retains storage and reflection's permission flags without boxing
// or reconstructing the value. Typed adapters use the same retained storage.
type valueHandle struct {
	object reflect.Value
	ops    *valueOperations
}

// valueOperations contains no checkpoint IDs or mutable per-save/load state.
// Child providers can refer back to a table without recursively constructing a
// descriptor graph. Tables supplied by a type must be immutable.
type valueOperations struct {
	kind reflect.Kind
	save func(*encodeState, valueHandle, encodeStrategy, *wire.Object)
	load func(*decodeState, *objectDecodeState, valueHandle, wire.Object)

	zero func(valueHandle)
	copy func(valueHandle) valueHandle

	mapStorage *mapStorage
	sequence   func(valueHandle) sequenceView
	// element supplies the pointee or slice element operation when statically
	// known. A nil provider resolves the operation at the dynamic boundary.
	element func() *valueOperations
	backing func() *valueOperations
}

// valueOperationProvider is deliberately internal while the common engine is
// qualified with a manual graph. Its receiver must be usable when nil; asking
// for a table must not register, allocate or traverse child types.
type valueOperationProvider interface {
	stateValueOperations() *valueOperations
}

func operationsFor(typ reflect.Type) *valueOperations {
	if typ.Name() != "" && typ.PkgPath() != "" {
		if p, ok := reflect.Zero(reflect.PointerTo(typ)).Interface().(valueOperationProvider); ok {
			return p.stateValueOperations()
		}
	}
	return &reflectedOperations[typ.Kind()]
}

func reflectedHandle(object reflect.Value) valueHandle {
	return handleWithOperations(object, nil)
}

func handleWithOperations(object reflect.Value, ops *valueOperations) valueHandle {
	if !object.IsValid() {
		return valueHandle{}
	}
	if !object.CanSet() {
		// Typed stores cannot bypass unaddressable or read-only values.
		ops = &reflectedOperations[object.Kind()]
	} else if ops == nil {
		ops = operationsFor(object.Type())
	}
	return valueHandle{object: object, ops: ops}
}

// pointerHandle selects a provider from the caller's existing pointer. It does
// not manufacture a reflected pointer type to ask the same question.
func pointerHandle(pointer any) valueHandle {
	object := reflect.ValueOf(pointer).Elem()
	if !object.IsValid() {
		return valueHandle{}
	}
	ops := &reflectedOperations[object.Kind()]
	if provider, ok := pointer.(valueOperationProvider); ok {
		ops = provider.stateValueOperations()
	}
	return valueHandle{object: object, ops: ops}
}

func typedHandle[T any](pointer *T, ops *valueOperations) valueHandle {
	return valueHandle{object: reflect.ValueOf(pointer).Elem(), ops: ops}
}

// reflected exposes layout and assignment metadata at graph, interface and
// compatibility boundaries. Scalar access and map insertion need not use it.
func (v valueHandle) reflected() reflect.Value {
	return v.object
}

func (v valueHandle) Type() reflect.Type { return v.reflected().Type() }
func (v valueHandle) Interface() any     { return v.reflected().Interface() }
func (v valueHandle) IsValid() bool      { return v.ops != nil }

func reflectedZero(v valueHandle) { v.reflected().SetZero() }

func reflectedCopy(v valueHandle) valueHandle {
	object := v.reflected()
	copy := reflect.New(object.Type()).Elem()
	copy.Set(object)
	return handleWithOperations(copy, v.ops)
}

// loadValue is the sole value entry. Nil and interface envelopes are common
// protocol behavior; the destination table owns the remaining operation.
func (ds *decodeState) loadValue(ods *objectDecodeState, target valueHandle, encoded wire.Object) {
	if _, ok := encoded.(wire.Nil); ok {
		return
	}
	if wrapped, ok := encoded.(*wire.Interface); ok {
		ds.decodeInterface(ods, target.reflected(), wrapped)
		return
	}
	if target.ops == nil || target.ops.load == nil {
		Failf("cannot restore %T into %s", encoded, target.Type())
	}
	target.ops.load(ds, ods, target, encoded)
}

// saveValue is the sole value entry. Graph references and collection policies
// are shared by every storage adapter.
func (es *encodeState) saveValue(value valueHandle, how encodeStrategy, dest *wire.Object) {
	if how == encodeDefault && isPrimitiveZero(value.Type()) && value.reflected().IsZero() {
		*dest = wire.Nil{}
		return
	}
	if value.ops == nil || value.ops.save == nil {
		Failf("unknown object %#v", value.Interface())
	}
	value.ops.save(es, value, how, dest)
}

func saveBool(_ *encodeState, v valueHandle, _ encodeStrategy, dest *wire.Object) {
	*dest = wire.Bool(v.object.Bool())
}
func loadBool(_ *decodeState, _ *objectDecodeState, v valueHandle, encoded wire.Object) {
	v.object.SetBool(bool(encoded.(wire.Bool)))
}
func saveInt(_ *encodeState, v valueHandle, _ encodeStrategy, dest *wire.Object) {
	*dest = wire.Int(v.object.Int())
}
func loadInt(_ *decodeState, _ *objectDecodeState, v valueHandle, encoded wire.Object) {
	x := int64(encoded.(wire.Int))
	v.object.SetInt(x)
	checkInt(x, v.object.Int())
}
func saveUint(_ *encodeState, v valueHandle, _ encodeStrategy, dest *wire.Object) {
	*dest = wire.Uint(v.object.Uint())
}
func loadUint(_ *decodeState, _ *objectDecodeState, v valueHandle, encoded wire.Object) {
	x := uint64(encoded.(wire.Uint))
	v.object.SetUint(x)
	checkUint(x, v.object.Uint())
}
func saveFloat(_ *encodeState, v valueHandle, _ encodeStrategy, dest *wire.Object) {
	if v.ops.kind == reflect.Float32 {
		*dest = wire.Float32(v.object.Float())
	} else {
		*dest = wire.Float64(v.object.Float())
	}
}
func loadFloat(_ *decodeState, _ *objectDecodeState, v valueHandle, encoded wire.Object) {
	if x, ok := encoded.(wire.Float32); ok {
		v.object.SetFloat(float64(x))
	} else {
		x := float64(encoded.(wire.Float64))
		v.object.SetFloat(x)
		checkFloat(x, v.object.Float())
	}
}
func saveComplex(_ *encodeState, v valueHandle, _ encodeStrategy, dest *wire.Object) {
	if v.ops.kind == reflect.Complex64 {
		x := wire.Complex64(v.object.Complex())
		*dest = &x
	} else {
		x := wire.Complex128(v.object.Complex())
		*dest = &x
	}
}
func loadComplex(_ *decodeState, _ *objectDecodeState, v valueHandle, encoded wire.Object) {
	if x, ok := encoded.(*wire.Complex64); ok {
		v.object.SetComplex(complex128(*x))
	} else {
		x := complex128(*encoded.(*wire.Complex128))
		v.object.SetComplex(x)
		checkComplex(x, v.object.Complex())
	}
}
func saveString(_ *encodeState, v valueHandle, _ encodeStrategy, dest *wire.Object) {
	x := wire.String(v.object.String())
	*dest = &x
}
func loadString(_ *decodeState, _ *objectDecodeState, v valueHandle, encoded wire.Object) {
	v.object.SetString(string(*encoded.(*wire.String)))
}

func savePointer(es *encodeState, v valueHandle, _ encodeStrategy, dest *wire.Object) {
	r := new(wire.Ref)
	*dest = r
	object := v.reflected()
	if !object.IsNil() {
		es.resolve(object, r, childOperations(v.ops.element))
	}
}
func loadPointer(ds *decodeState, _ *objectDecodeState, v valueHandle, encoded wire.Object) {
	ds.decodeReference(v, encoded.(*wire.Ref))
}
func saveSlice(es *encodeState, v valueHandle, _ encodeStrategy, dest *wire.Object) {
	object := v.reflected()
	s := &wire.Slice{Capacity: wire.Uint(object.Cap()), Length: wire.Uint(object.Len())}
	*dest = s
	if !object.IsNil() {
		es.resolve(arrayFromSlice(object), &s.Ref, childOperations(v.ops.backing))
	}
}
func loadSlice(ds *decodeState, ods *objectDecodeState, v valueHandle, encoded wire.Object) {
	if slice, ok := encoded.(*wire.Slice); ok {
		if objectID(slice.Ref.Root) != 0 {
			object := v.reflected()
			typ := reflect.ArrayOf(int(slice.Capacity), object.Type().Elem())
			backing := ds.register(&slice.Ref, typ, childOperations(v.ops.backing))
			object.Set(reflectValueRWSlice3(backing, 0, int(slice.Length), int(slice.Capacity)))
		}
	} else {
		ds.decodeArray(ods, v, encoded.(wire.Array))
	}
}
func saveArray(es *encodeState, v valueHandle, _ encodeStrategy, dest *wire.Object) {
	es.encodeArray(v, dest)
}
func loadArray(ds *decodeState, ods *objectDecodeState, v valueHandle, encoded wire.Object) {
	ds.decodeArray(ods, v, encoded.(wire.Array))
}
func saveStruct(es *encodeState, v valueHandle, _ encodeStrategy, dest *wire.Object) {
	es.encodeStruct(v.reflected(), dest)
}
func loadStruct(ds *decodeState, ods *objectDecodeState, v valueHandle, encoded wire.Object) {
	ds.decodeStruct(ods, v.reflected(), encoded.(*wire.Struct))
}
func saveInterface(es *encodeState, v valueHandle, _ encodeStrategy, dest *wire.Object) {
	es.encodeInterface(v.reflected(), dest)
}
func saveMap(es *encodeState, v valueHandle, how encodeStrategy, dest *wire.Object) {
	if how == encodeMapAsValue {
		es.encodeMap(v, dest)
	} else {
		r := new(wire.Ref)
		*dest = r
		es.resolve(v.reflected(), r, v.ops)
	}
}

func childOperations(provider func() *valueOperations) *valueOperations {
	if provider == nil {
		return nil
	}
	return provider()
}
func loadMap(ds *decodeState, ods *objectDecodeState, v valueHandle, encoded wire.Object) {
	if ref, ok := encoded.(*wire.Ref); ok {
		ds.decodeReference(v, ref)
	} else {
		ds.decodeMap(ods, v, encoded.(*wire.Map))
	}
}
