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
		if encoded, ok := s.internal.encoded.Scalar(s.internal.rte.FieldOrder[slot]); ok && loadCaptureScalar(s.internal.rte.fieldDescriptors[slot].kind, unsafe.Pointer(value), encoded) {
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
	var encoded wire.Scalar
	switch s.internal.fields[slot].kind {
	case reflect.Bool:
		if !*(*bool)(p) {
			encoded = wire.Scalar{Kind: wire.ScalarNil}
		} else {
			encoded = wire.Scalar{Kind: wire.ScalarBool, Uint: 1}
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
			encoded = wire.Scalar{Kind: wire.ScalarNil}
		} else {
			encoded = wire.Scalar{Kind: wire.ScalarInt, Int: x}
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
			encoded = wire.Scalar{Kind: wire.ScalarNil}
		} else {
			encoded = wire.Scalar{Kind: wire.ScalarUint, Uint: x}
		}
	case reflect.String:
		x := *(*string)(p)
		if x == "" {
			encoded = wire.Scalar{Kind: wire.ScalarNil}
		} else {
			encoded = wire.Scalar{Kind: wire.ScalarString, Text: x}
		}
	case reflect.Float32:
		x := *(*float32)(p)
		if x == 0 {
			encoded = wire.Scalar{Kind: wire.ScalarNil}
		} else {
			encoded = wire.Scalar{Kind: wire.ScalarFloat32, Real: float64(x)}
		}
	case reflect.Float64:
		x := *(*float64)(p)
		if x == 0 {
			encoded = wire.Scalar{Kind: wire.ScalarNil}
		} else {
			encoded = wire.Scalar{Kind: wire.ScalarFloat64, Real: x}
		}
	case reflect.Complex64:
		x := *(*complex64)(p)
		if x == 0 {
			encoded = wire.Scalar{Kind: wire.ScalarNil}
		} else {
			encoded = wire.Scalar{Kind: wire.ScalarComplex64, Real: float64(real(x)), Imag: float64(imag(x))}
		}
	case reflect.Complex128:
		x := *(*complex128)(p)
		if x == 0 {
			encoded = wire.Scalar{Kind: wire.ScalarNil}
		} else {
			encoded = wire.Scalar{Kind: wire.ScalarComplex128, Real: real(x), Imag: imag(x)}
		}
	default:
		return false
	}
	s.internal.encoded.StoreScalar(slot, encoded)
	return true
}

func loadCaptureScalar(kind reflect.Kind, p unsafe.Pointer, value wire.Scalar) bool {
	switch value.Kind {
	case wire.ScalarNil:
		return true
	case wire.ScalarBool:
		if kind != reflect.Bool {
			return false
		}
		*(*bool)(p) = value.Uint == 1
	case wire.ScalarInt:
		var decoded int64
		switch kind {
		case reflect.Int:
			*(*int)(p) = int(value.Int)
			decoded = int64(*(*int)(p))
		case reflect.Int8:
			*(*int8)(p) = int8(value.Int)
			decoded = int64(*(*int8)(p))
		case reflect.Int16:
			*(*int16)(p) = int16(value.Int)
			decoded = int64(*(*int16)(p))
		case reflect.Int32:
			*(*int32)(p) = int32(value.Int)
			decoded = int64(*(*int32)(p))
		case reflect.Int64:
			*(*int64)(p) = value.Int
			decoded = value.Int
		default:
			return false
		}
		if decoded != value.Int {
			Failf("signed integer truncated from %v to %v", value.Int, decoded)
		}
	case wire.ScalarUint:
		var decoded uint64
		switch kind {
		case reflect.Uint:
			*(*uint)(p) = uint(value.Uint)
			decoded = uint64(*(*uint)(p))
		case reflect.Uint8:
			*(*uint8)(p) = uint8(value.Uint)
			decoded = uint64(*(*uint8)(p))
		case reflect.Uint16:
			*(*uint16)(p) = uint16(value.Uint)
			decoded = uint64(*(*uint16)(p))
		case reflect.Uint32:
			*(*uint32)(p) = uint32(value.Uint)
			decoded = uint64(*(*uint32)(p))
		case reflect.Uint64:
			*(*uint64)(p) = value.Uint
			decoded = value.Uint
		case reflect.Uintptr:
			*(*uintptr)(p) = uintptr(value.Uint)
			decoded = uint64(*(*uintptr)(p))
		default:
			return false
		}
		if decoded != value.Uint {
			Failf("unsigned integer truncated from %v to %v", value.Uint, decoded)
		}
	case wire.ScalarString:
		if kind != reflect.String {
			return false
		}
		*(*string)(p) = value.Text
	case wire.ScalarFloat32, wire.ScalarFloat64:
		var decoded float64
		switch kind {
		case reflect.Float32:
			*(*float32)(p) = float32(value.Real)
			decoded = float64(*(*float32)(p))
		case reflect.Float64:
			*(*float64)(p) = value.Real
			decoded = value.Real
		default:
			return false
		}
		if value.Kind == wire.ScalarFloat64 && !isFloatEq(decoded, value.Real) {
			Failf("floating point number truncated from %v to %v", value.Real, decoded)
		}
	case wire.ScalarComplex64, wire.ScalarComplex128:
		x := complex(value.Real, value.Imag)
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
		if value.Kind == wire.ScalarComplex128 && !isComplexEq(decoded, x) {
			Failf("complex number truncated from %v to %v", x, decoded)
		}
	default:
		return false
	}
	return true
}
