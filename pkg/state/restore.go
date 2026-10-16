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

// Generated scalar fields retain their static type through assignment. The
// Value accessors and truncation checks are shared with restoreValue. A scalar
// has no graph dependency; other encodings, including nil and interfaces,
// return to the ordinary Source path with its original wait semantics.

// LoadBool restores a generated boolean field.
func LoadBool[T ~bool](s Source, slot int, ptr *T, wait bool) {
	if encoded := dataValue(s.internal.field(slot)); encoded.Kind() == wire.KindBool {
		*ptr = T(encoded.Bool())
		return
	}
	s.internal.load(slot, reflect.ValueOf(ptr), wait, nil)
}

// LoadInt restores a generated signed integer field, rejecting truncation.
func LoadInt[T ~int | ~int8 | ~int16 | ~int32 | ~int64](s Source, slot int, ptr *T, wait bool) {
	if encoded := dataValue(s.internal.field(slot)); encoded.Kind() == wire.KindInt {
		value := encoded.Int()
		*ptr = T(value)
		checkInt(value, int64(*ptr))
		return
	}
	s.internal.load(slot, reflect.ValueOf(ptr), wait, nil)
}

// LoadUint restores a generated unsigned integer field, rejecting truncation.
func LoadUint[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr](s Source, slot int, ptr *T, wait bool) {
	if encoded := dataValue(s.internal.field(slot)); encoded.Kind() == wire.KindUint {
		value := encoded.Uint()
		*ptr = T(value)
		checkUint(value, uint64(*ptr))
		return
	}
	s.internal.load(slot, reflect.ValueOf(ptr), wait, nil)
}

// LoadFloat restores a generated floating-point field with the ordinary NaN
// and truncation rules.
func LoadFloat[T ~float32 | ~float64](s Source, slot int, ptr *T, wait bool) {
	if encoded := dataValue(s.internal.field(slot)); encoded.Kind() == wire.KindFloat32 || encoded.Kind() == wire.KindFloat64 {
		value := encoded.Float()
		*ptr = T(value)
		checkFloat(value, float64(*ptr))
		return
	}
	s.internal.load(slot, reflect.ValueOf(ptr), wait, nil)
}

// LoadComplex restores a generated complex field with the ordinary NaN and
// truncation rules for both components.
func LoadComplex[T ~complex64 | ~complex128](s Source, slot int, ptr *T, wait bool) {
	if encoded := dataValue(s.internal.field(slot)); encoded.Kind() == wire.KindComplex64 || encoded.Kind() == wire.KindComplex128 {
		value := encoded.Complex()
		*ptr = T(value)
		checkComplex(value, complex128(*ptr))
		return
	}
	s.internal.load(slot, reflect.ValueOf(ptr), wait, nil)
}

// LoadString restores a generated string field through an ordinary typed
// assignment, including the Go write barrier.
func LoadString[T ~string](s Source, slot int, ptr *T, wait bool) {
	if encoded := dataValue(s.internal.field(slot)); encoded.Kind() == wire.KindString {
		*ptr = T(encoded.Text())
		return
	}
	s.internal.load(slot, reflect.ValueOf(ptr), wait, nil)
}

// dataValue preserves nil-interface delegation before any address or view is
// required. Wait registration still receives the original parsed value.
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

// restoreValue owns every encoded-kind operation, for generated fields,
// manual Source calls, graph objects and dynamic interface/map storage alike.
func (ds *decodeState) restoreValue(ods *objectDecodeState, target restoreDestination, encoded wire.Value) {
	encoded = dataValue(encoded)
	switch encoded.Kind() {
	case wire.KindNil:
		return
	case wire.KindRef:
		ds.restoreReference(target, encoded.Reference())
	case wire.KindBool:
		target.storeBool(encoded.Bool())
	case wire.KindInt:
		value := encoded.Int()
		checkInt(value, target.storeInt(value))
	case wire.KindUint:
		value := encoded.Uint()
		checkUint(value, target.storeUint(value))
	case wire.KindFloat32:
		target.storeFloat(encoded.Float())
	case wire.KindFloat64:
		value := encoded.Float()
		checkFloat(value, target.storeFloat(value))
	case wire.KindComplex64:
		target.storeComplex(encoded.Complex())
	case wire.KindComplex128:
		value := encoded.Complex()
		checkComplex(value, target.storeComplex(value))
	case wire.KindString:
		target.storeString(encoded.Text())
	case wire.KindSlice:
		if slice := encoded.Slice(); objectID(slice.Ref.Root) != 0 {
			ds.assignSlice(target.value(), slice)
		}
	case wire.KindArray:
		ds.restoreArray(ods, target, encoded)
	case wire.KindMap:
		ds.restoreMap(ods, target, encoded)
	case wire.KindStruct:
		ds.decodeStruct(ods, target, encoded)
	case wire.KindInterface:
		ds.decodeInterface(ods, target.value(), encoded)
	default:
		Failf("unknown object %#v for %q", encoded, target.value().Type().Name())
	}
}

// restoreReference retains register/Dots/assignment order. Only ordinary
// pointer stores bypass reflect.Set; maps retain actual source flags and type.
func (ds *decodeState) restoreReference(target restoreDestination, ref wire.Ref) {
	if objectID(ref.Root) == 0 {
		return
	}
	if target.typ == nil {
		target.value().Type() // Preserve the invalid destination error.
	}
	if target.typ.Kind() == reflect.Map {
		object := walkChild(ref, reflect.ValueOf(ds.register(ref, target.typ)).Elem())
		assignMap(target.value(), object)
		return
	}
	pointer := reflect.ValueOf(ds.register(ref, target.typ.Elem()))
	if len(ref.Dots) != 0 {
		pointer = reflectValueRWAddr(walkChild(ref, pointer.Elem()))
	}
	if pointer.IsNil() {
		Failf("reference %d has no object storage", ref.Root)
	}
	target.storePointer(pointer)
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
