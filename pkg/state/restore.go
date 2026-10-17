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

// restoreValue owns every encoded-kind operation, for generated fields,
// manual Source calls, graph objects and dynamic interface/map storage alike.
func (ds *decodeState) restoreValue(ods *objectDecodeState, target reflect.Value, encoded wire.Object) {
	switch value := dataValue(encoded).(type) {
	case wire.Nil:
		return
	case *wire.Ref:
		ds.restoreReference(target, value)
	case wire.Bool:
		target.SetBool(bool(value))
	case wire.Int:
		target.SetInt(int64(value))
		checkInt(int64(value), target.Int())
	case wire.Uint:
		target.SetUint(uint64(value))
		checkUint(uint64(value), target.Uint())
	case wire.Float32:
		target.SetFloat(float64(value))
	case wire.Float64:
		target.SetFloat(float64(value))
		checkFloat(float64(value), target.Float())
	case *wire.Complex64:
		target.SetComplex(complex128(*value))
	case *wire.Complex128:
		target.SetComplex(complex128(*value))
		checkComplex(complex128(*value), target.Complex())
	case *wire.String:
		target.SetString(string(*value))
	case *wire.Slice:
		if objectID(value.Ref.Root) != 0 {
			ds.assignSlice(target, value)
		}
	case wire.Array:
		ds.restoreArray(ods, target, value)
	case *wire.Map:
		ds.restoreMap(ods, target, value)
	case *wire.Struct:
		ds.decodeStruct(ods, target, value)
	case *wire.Interface:
		ds.decodeInterface(ods, target, value)
	default:
		Failf("unknown object %#v for %q", encoded, target.Type().Name())
	}
}

// restoreReference retains register/Dots/assignment order and the destination's
// reflected assignment rules. Interior pointers alone need writable addresses.
func (ds *decodeState) restoreReference(target reflect.Value, ref *wire.Ref) {
	if objectID(ref.Root) == 0 {
		return
	}
	if target.Kind() == reflect.Map {
		object := walkChild(ref, ds.register(ref, target.Type()))
		assignMap(target, object)
		return
	}
	object := ds.register(ref, target.Type().Elem())
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
