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
// concrete wire payload and truncation rules are shared with restoreValue.
// Other encodings, including nil and interfaces, use the ordinary Source path
// with its original wait semantics.

// LoadBool restores a generated boolean field.
func LoadBool[T ~bool](s Source, slot int, ptr *T, wait bool) {
	if encoded, ok := dataValue(s.internal.field(slot)).(wire.Bool); ok {
		*ptr = T(encoded)
		return
	}
	s.internal.load(slot, reflect.ValueOf(ptr), wait, nil)
}

// LoadInt restores a generated signed integer field, rejecting truncation.
func LoadInt[T ~int | ~int8 | ~int16 | ~int32 | ~int64](s Source, slot int, ptr *T, wait bool) {
	if encoded, ok := dataValue(s.internal.field(slot)).(wire.Int); ok {
		*ptr = T(encoded)
		checkInt(int64(encoded), int64(*ptr))
		return
	}
	s.internal.load(slot, reflect.ValueOf(ptr), wait, nil)
}

// LoadUint restores a generated unsigned integer field, rejecting truncation.
func LoadUint[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr](s Source, slot int, ptr *T, wait bool) {
	if encoded, ok := dataValue(s.internal.field(slot)).(wire.Uint); ok {
		*ptr = T(encoded)
		checkUint(uint64(encoded), uint64(*ptr))
		return
	}
	s.internal.load(slot, reflect.ValueOf(ptr), wait, nil)
}

// LoadFloat restores a generated floating-point field with the ordinary NaN
// and truncation rules.
func LoadFloat[T ~float32 | ~float64](s Source, slot int, ptr *T, wait bool) {
	var value float64
	switch encoded := dataValue(s.internal.field(slot)).(type) {
	case wire.Float32:
		value = float64(encoded)
	case wire.Float64:
		value = float64(encoded)
	default:
		s.internal.load(slot, reflect.ValueOf(ptr), wait, nil)
		return
	}
	*ptr = T(value)
	checkFloat(value, float64(*ptr))
}

// LoadComplex restores a generated complex field with the ordinary NaN and
// truncation rules for both components.
func LoadComplex[T ~complex64 | ~complex128](s Source, slot int, ptr *T, wait bool) {
	var value complex128
	switch encoded := dataValue(s.internal.field(slot)).(type) {
	case *wire.Complex64:
		value = complex128(*encoded)
	case *wire.Complex128:
		value = complex128(*encoded)
	default:
		s.internal.load(slot, reflect.ValueOf(ptr), wait, nil)
		return
	}
	*ptr = T(value)
	checkComplex(value, complex128(*ptr))
}

// LoadString restores a generated string field through an ordinary typed
// assignment, including the Go write barrier.
func LoadString[T ~string](s Source, slot int, ptr *T, wait bool) {
	if encoded, ok := dataValue(s.internal.field(slot)).(*wire.String); ok {
		*ptr = T(*encoded)
		return
	}
	s.internal.load(slot, reflect.ValueOf(ptr), wait, nil)
}

// dataValue preserves nil-interface delegation before any address is required.
// Wait registration still receives the original parsed object.
func dataValue(value wire.Object) wire.Object {
	for {
		wrapped, ok := value.(*wire.Interface)
		if !ok {
			return value
		}
		if _, ok := wrapped.Type.(wire.TypeSpecNil); !ok {
			return value
		}
		value = wrapped.Value
	}
}

// restoreValue owns every encoded-kind operation, for generated fields,
// manual Source calls, graph objects and dynamic interface/map storage alike.
func (ds *decodeState) restoreValue(ods *objectDecodeState, target restoreDestination, encoded wire.Object) {
	switch value := dataValue(encoded).(type) {
	case wire.Nil:
		return
	case *wire.Ref:
		ds.restoreReference(target, value)
	case wire.Bool:
		target.storeBool(bool(value))
	case wire.Int:
		checkInt(int64(value), target.storeInt(int64(value)))
	case wire.Uint:
		checkUint(uint64(value), target.storeUint(uint64(value)))
	case wire.Float32:
		target.storeFloat(float64(value))
	case wire.Float64:
		checkFloat(float64(value), target.storeFloat(float64(value)))
	case *wire.Complex64:
		target.storeComplex(complex128(*value))
	case *wire.Complex128:
		checkComplex(complex128(*value), target.storeComplex(complex128(*value)))
	case *wire.String:
		target.storeString(string(*value))
	case *wire.Slice:
		if objectID(value.Ref.Root) != 0 {
			ds.assignSlice(target.value(), value)
		}
	case *wire.Array:
		ds.restoreArray(ods, target, value.Contents)
	case *wire.Map:
		ds.restoreMap(ods, target, value)
	case *wire.Struct:
		ds.decodeStruct(ods, target, value)
	case *wire.Interface:
		ds.decodeInterface(ods, target.value(), value)
	default:
		Failf("unknown object %#v for %q", encoded, target.value().Type().Name())
	}
}

// restoreReference retains register/Dots/assignment order. Only ordinary
// pointer stores bypass reflect.Set; maps retain actual source flags and type.
func (ds *decodeState) restoreReference(target restoreDestination, ref *wire.Ref) {
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
