// Copyright 2018 The gVisor Authors.
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

// SaveField captures generated primitive fields without constructing a
// reflect.Value or boxed wire.Object. The pointer remains borrowed; strings
// are copied into the capture. Composite and custom fields use the shared graph.
func SaveField[T any](s Sink, slot int, value *T) {
	if s.internal.encoded.IsCapture() && capturePrimitive(s.internal.encoded, slot, unsafe.Pointer(value), reflect.TypeFor[T]().Kind()) {
		return
	}
	s.Save(slot, value)
}

func capturePrimitive(s *wire.Struct, slot int, p unsafe.Pointer, kind reflect.Kind) bool {
	switch kind {
	case reflect.Bool:
		s.CaptureBool(slot, *(*bool)(p))
	case reflect.Int:
		s.CaptureInt(slot, int64(*(*int)(p)))
	case reflect.Int8:
		s.CaptureInt(slot, int64(*(*int8)(p)))
	case reflect.Int16:
		s.CaptureInt(slot, int64(*(*int16)(p)))
	case reflect.Int32:
		s.CaptureInt(slot, int64(*(*int32)(p)))
	case reflect.Int64:
		s.CaptureInt(slot, *(*int64)(p))
	case reflect.Uint:
		s.CaptureUint(slot, uint64(*(*uint)(p)))
	case reflect.Uint8:
		s.CaptureUint(slot, uint64(*(*uint8)(p)))
	case reflect.Uint16:
		s.CaptureUint(slot, uint64(*(*uint16)(p)))
	case reflect.Uint32:
		s.CaptureUint(slot, uint64(*(*uint32)(p)))
	case reflect.Uint64:
		s.CaptureUint(slot, *(*uint64)(p))
	case reflect.Uintptr:
		s.CaptureUint(slot, uint64(*(*uintptr)(p)))
	case reflect.Float32:
		s.CaptureFloat32(slot, float64(*(*float32)(p)))
	case reflect.Float64:
		s.CaptureFloat64(slot, *(*float64)(p))
	case reflect.Complex64:
		s.CaptureComplex64(slot, complex128(*(*complex64)(p)))
	case reflect.Complex128:
		s.CaptureComplex128(slot, *(*complex128)(p))
	case reflect.String:
		s.CaptureString(slot, *(*string)(p))
	default:
		return false
	}
	return true
}
