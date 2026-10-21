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

// restoreValue adapts graph storage to the same pointer-based destination
// decoder used by Source.Load. The pointer continues to identify the graph's
// authoritative allocation.
func (ds *decodeState) restoreValue(ods *objectDecodeState, target reflect.Value, encoded wire.Object) {
	// Nil needs no storage. A zero-length array can be represented by Nil
	// even when the caller supplies a nil pointer to that array.
	if _, ok := dataValue(encoded).(wire.Nil); ok {
		return
	}
	if !target.CanAddr() || !target.CanInterface() {
		Failf("destination %s cannot be addressed", target.Type())
	}
	ds.decoders.lookup(target.Type()).restore(ds, ods, target.Addr().Interface(), encoded)
}

func requireWire[T wire.Object](value wire.Object) T {
	v, ok := value.(T)
	if !ok {
		Failf("expected %s, got %T", reflect.TypeFor[T](), value)
	}
	return v
}

func boolDecoder(store func(any, bool)) func(*decodeState, *objectDecodeState, any, wire.Object) {
	return func(_ *decodeState, _ *objectDecodeState, p any, value wire.Object) {
		store(p, bool(requireWire[wire.Bool](value)))
	}
}

func intDecoder(store func(any, int64) int64) func(*decodeState, *objectDecodeState, any, wire.Object) {
	return func(_ *decodeState, _ *objectDecodeState, p any, value wire.Object) {
		v := int64(requireWire[wire.Int](value))
		checkInt(v, store(p, v))
	}
}

func uintDecoder(store func(any, uint64) uint64) func(*decodeState, *objectDecodeState, any, wire.Object) {
	return func(_ *decodeState, _ *objectDecodeState, p any, value wire.Object) {
		v := uint64(requireWire[wire.Uint](value))
		checkUint(v, store(p, v))
	}
}

func floatDecoder(store func(any, float64) float64) func(*decodeState, *objectDecodeState, any, wire.Object) {
	return func(_ *decodeState, _ *objectDecodeState, p any, value wire.Object) {
		if v, ok := value.(wire.Float32); ok {
			store(p, float64(v))
			return
		}
		v := float64(requireWire[wire.Float64](value))
		checkFloat(v, store(p, v))
	}
}

func complexDecoder(store func(any, complex128) complex128) func(*decodeState, *objectDecodeState, any, wire.Object) {
	return func(_ *decodeState, _ *objectDecodeState, p any, value wire.Object) {
		if v, ok := value.(*wire.Complex64); ok {
			store(p, complex128(*v))
			return
		}
		v := complex128(*requireWire[*wire.Complex128](value))
		checkComplex(v, store(p, v))
	}
}

func stringDecoder(store func(any, string)) func(*decodeState, *objectDecodeState, any, wire.Object) {
	return func(_ *decodeState, _ *objectDecodeState, p any, value wire.Object) {
		store(p, string(*requireWire[*wire.String](value)))
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
