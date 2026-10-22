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

// restoreValue selects the operation from the destination's Go type and checks
// that the encoded value has the corresponding wire representation. Generated
// fields, manual loaders and dynamically allocated graph objects share this path.
func (ds *decodeState) restoreValue(ods *objectDecodeState, target reflect.Value, encoded wire.Object) {
	value := dataValue(encoded)
	if _, ok := value.(wire.Nil); ok {
		return
	}
	if wrapped, ok := value.(*wire.Interface); ok {
		// Manual loaders may unwrap an interface into its concrete type.
		// decodeInterface checks assignability at that boundary.
		ds.decodeInterface(ods, target, wrapped)
		return
	}
	switch target.Kind() {
	case reflect.Bool:
		target.SetBool(bool(requireWire[wire.Bool](value)))
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		v := int64(requireWire[wire.Int](value))
		target.SetInt(v)
		checkInt(v, target.Int())
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
		v := uint64(requireWire[wire.Uint](value))
		target.SetUint(v)
		checkUint(v, target.Uint())
	case reflect.Float32, reflect.Float64:
		if v, ok := value.(wire.Float32); ok {
			target.SetFloat(float64(v))
		} else {
			v := float64(requireWire[wire.Float64](value))
			target.SetFloat(v)
			checkFloat(v, target.Float())
		}
	case reflect.Complex64, reflect.Complex128:
		if v, ok := value.(*wire.Complex64); ok {
			target.SetComplex(complex128(*v))
		} else {
			v := complex128(*requireWire[*wire.Complex128](value))
			target.SetComplex(v)
			checkComplex(v, target.Complex())
		}
	case reflect.String:
		target.SetString(string(*requireWire[*wire.String](value)))
	case reflect.Pointer:
		ds.restoreReference(target, requireWire[*wire.Ref](value), nil)
	case reflect.Slice:
		if slice, ok := value.(*wire.Slice); ok {
			ds.assignSlice(target, slice, nil)
		} else {
			// Manual savers can encode an array into an existing slice.
			ds.restoreArray(ods, target, requireWire[wire.Array](value))
		}
	case reflect.Array:
		ds.restoreArray(ods, target, requireWire[wire.Array](value))
	case reflect.Map:
		if ref, ok := value.(*wire.Ref); ok {
			ds.restoreReference(target, ref, nil)
		} else {
			ds.restoreMap(ods, target, requireWire[*wire.Map](value))
		}
	case reflect.Struct:
		ds.decodeStruct(ods, target.Type(), target.Addr().Interface(), requireWire[*wire.Struct](value))
	default:
		Failf("cannot restore %T into %s", value, target.Type())
	}
}

func requireWire[T wire.Object](value wire.Object) T {
	v, ok := value.(T)
	if !ok {
		Failf("expected %s, got %T", reflect.TypeFor[T](), value)
	}
	return v
}

// restoreReference retains register/Dots/assignment order and the destination's
// reflected assignment rules. Interior pointers alone need writable addresses.
func (ds *decodeState) restoreReference(target reflect.Value, ref *wire.Ref, body objectBody) {
	if objectID(ref.Root) == 0 {
		return
	}
	if target.Kind() == reflect.Map {
		object := walkChild(ref, ds.register(ref, target.Type(), body))
		assignMap(target, object)
		return
	}
	object := ds.register(ref, target.Type().Elem(), body)
	var pointer reflect.Value
	if len(ref.Dots) != 0 {
		pointer = reflectValueRWAddr(walkChild(ref, object))
	} else {
		pointer = object.Addr()
	}
	target.Set(pointer)
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
