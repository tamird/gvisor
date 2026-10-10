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
	"math"
	"reflect"
	"unsafe"

	"gvisor.dev/gvisor/pkg/state/wire"
)

// SaveField captures a generated field using the containing type's descriptor.
// T keeps the call type-checked; the shared implementation does not instantiate
// a scalar adapter for every field type. The pointer is not retained.
func SaveField[T any](s Sink, slot int, value *T) {
	if s.internal.es.captures != nil && s.internal.fields[slot].typ == reflect.TypeFor[T]() && saveCaptureScalar(s, slot, unsafe.Pointer(value)) {
		return
	}
	s.Save(slot, value)
}

// LoadField loads a generated field directly from captured storage when it is
// a primitive. Dynamic forms return to the original T for assignment checking.
func LoadField[T any](s Source, slot int, value *T, wait bool) {
	if s.internal.ds.direct && s.internal.rte.fieldDescriptors[slot].typ == reflect.TypeFor[T]() {
		if loadCaptureScalar(s.internal.rte.fieldDescriptors[slot].kind, unsafe.Pointer(value), s.internal.encoded, s.internal.rte.FieldOrder[slot]) {
			// Primitive values contain no references and add no hook dependency.
			return
		}
	}
	if wait {
		s.LoadWait(slot, value)
	} else {
		s.Load(slot, value)
	}
}

func saveCaptureScalar(s Sink, slot int, p unsafe.Pointer) bool {
	encoded := s.internal.encoded
	switch s.internal.fields[slot].kind {
	case reflect.Bool:
		if !*(*bool)(p) {
			encoded.StoreNil(slot)
		} else {
			encoded.StoreWord(slot, wire.ScalarBool, 1)
		}
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		var x int64
		switch s.internal.fields[slot].kind {
		case reflect.Int:
			x = int64(*(*int)(p))
		case reflect.Int8:
			x = int64(*(*int8)(p))
		case reflect.Int16:
			x = int64(*(*int16)(p))
		case reflect.Int32:
			x = int64(*(*int32)(p))
		case reflect.Int64:
			x = *(*int64)(p)
		}
		if x == 0 {
			encoded.StoreNil(slot)
		} else {
			encoded.StoreWord(slot, wire.ScalarInt, uint64(x))
		}
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
		var x uint64
		switch s.internal.fields[slot].kind {
		case reflect.Uint:
			x = uint64(*(*uint)(p))
		case reflect.Uint8:
			x = uint64(*(*uint8)(p))
		case reflect.Uint16:
			x = uint64(*(*uint16)(p))
		case reflect.Uint32:
			x = uint64(*(*uint32)(p))
		case reflect.Uint64:
			x = *(*uint64)(p)
		case reflect.Uintptr:
			x = uint64(*(*uintptr)(p))
		}
		if x == 0 {
			encoded.StoreNil(slot)
		} else {
			encoded.StoreWord(slot, wire.ScalarUint, x)
		}
	case reflect.String:
		x := *(*string)(p)
		if x == "" {
			encoded.StoreNil(slot)
		} else {
			encoded.StoreString(slot, x)
		}
	case reflect.Float32:
		x := *(*float32)(p)
		if x == 0 {
			encoded.StoreNil(slot)
		} else {
			encoded.StoreWord(slot, wire.ScalarFloat32, math.Float64bits(float64(x)))
		}
	case reflect.Float64:
		x := *(*float64)(p)
		if x == 0 {
			encoded.StoreNil(slot)
		} else {
			encoded.StoreWord(slot, wire.ScalarFloat64, math.Float64bits(x))
		}
	case reflect.Complex64:
		x := *(*complex64)(p)
		if x == 0 {
			encoded.StoreNil(slot)
		} else {
			encoded.StoreComplex(slot, wire.ScalarComplex64, complex128(x))
		}
	case reflect.Complex128:
		x := *(*complex128)(p)
		if x == 0 {
			encoded.StoreNil(slot)
		} else {
			encoded.StoreComplex(slot, wire.ScalarComplex128, x)
		}
	default:
		return false
	}
	return true
}

func loadCaptureScalar(kind reflect.Kind, p unsafe.Pointer, encoded *wire.Struct, slot int) bool {
	wireKind, ok := encoded.ScalarKind(slot)
	if !ok {
		return false
	}
	switch wireKind {
	case wire.ScalarNil:
		return true
	case wire.ScalarBool:
		if kind != reflect.Bool {
			return false
		}
		*(*bool)(p) = encoded.ScalarWord(slot) == 1
	case wire.ScalarInt:
		x := int64(encoded.ScalarWord(slot))
		var decoded int64
		switch kind {
		case reflect.Int:
			*(*int)(p) = int(x)
			decoded = int64(*(*int)(p))
		case reflect.Int8:
			*(*int8)(p) = int8(x)
			decoded = int64(*(*int8)(p))
		case reflect.Int16:
			*(*int16)(p) = int16(x)
			decoded = int64(*(*int16)(p))
		case reflect.Int32:
			*(*int32)(p) = int32(x)
			decoded = int64(*(*int32)(p))
		case reflect.Int64:
			*(*int64)(p) = x
			decoded = x
		default:
			return false
		}
		if decoded != x {
			Failf("signed integer truncated from %v to %v", x, decoded)
		}
	case wire.ScalarUint:
		x := encoded.ScalarWord(slot)
		var decoded uint64
		switch kind {
		case reflect.Uint:
			*(*uint)(p) = uint(x)
			decoded = uint64(*(*uint)(p))
		case reflect.Uint8:
			*(*uint8)(p) = uint8(x)
			decoded = uint64(*(*uint8)(p))
		case reflect.Uint16:
			*(*uint16)(p) = uint16(x)
			decoded = uint64(*(*uint16)(p))
		case reflect.Uint32:
			*(*uint32)(p) = uint32(x)
			decoded = uint64(*(*uint32)(p))
		case reflect.Uint64:
			*(*uint64)(p) = x
			decoded = x
		case reflect.Uintptr:
			*(*uintptr)(p) = uintptr(x)
			decoded = uint64(*(*uintptr)(p))
		default:
			return false
		}
		if decoded != x {
			Failf("unsigned integer truncated from %v to %v", x, decoded)
		}
	case wire.ScalarString:
		if kind != reflect.String {
			return false
		}
		*(*string)(p) = encoded.ScalarString(slot)
	case wire.ScalarFloat32, wire.ScalarFloat64:
		x := math.Float64frombits(encoded.ScalarWord(slot))
		var decoded float64
		switch kind {
		case reflect.Float32:
			*(*float32)(p) = float32(x)
			decoded = float64(*(*float32)(p))
		case reflect.Float64:
			*(*float64)(p) = x
			decoded = x
		default:
			return false
		}
		if wireKind == wire.ScalarFloat64 && !isFloatEq(decoded, x) {
			Failf("floating point number truncated from %v to %v", x, decoded)
		}
	case wire.ScalarComplex64, wire.ScalarComplex128:
		x := encoded.ScalarComplex(slot)
		var decoded complex128
		switch kind {
		case reflect.Complex64:
			*(*complex64)(p) = complex64(x)
			decoded = complex128(*(*complex64)(p))
		case reflect.Complex128:
			*(*complex128)(p) = x
			decoded = x
		default:
			return false
		}
		if wireKind == wire.ScalarComplex128 && !isComplexEq(decoded, x) {
			Failf("complex number truncated from %v to %v", x, decoded)
		}
	default:
		return false
	}
	return true
}
