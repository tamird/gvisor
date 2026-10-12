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

// LoadBool loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type bool.
//   - The storage remains live through this call.
func (s *Source) LoadBool(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeBool((*bool)(dst), encoded) {
		Failf("wire value has incompatible bool destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadInt loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type int.
//   - The storage remains live through this call.
func (s *Source) LoadInt(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeSigned((*int)(dst), encoded) {
		Failf("wire value has incompatible int destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadInt8 loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type int8.
//   - The storage remains live through this call.
func (s *Source) LoadInt8(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeSigned((*int8)(dst), encoded) {
		Failf("wire value has incompatible int8 destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadInt16 loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type int16.
//   - The storage remains live through this call.
func (s *Source) LoadInt16(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeSigned((*int16)(dst), encoded) {
		Failf("wire value has incompatible int16 destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadInt32 loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type int32.
//   - The storage remains live through this call.
func (s *Source) LoadInt32(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeSigned((*int32)(dst), encoded) {
		Failf("wire value has incompatible int32 destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadInt64 loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type int64.
//   - The storage remains live through this call.
func (s *Source) LoadInt64(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeSigned((*int64)(dst), encoded) {
		Failf("wire value has incompatible int64 destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadUint loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type uint.
//   - The storage remains live through this call.
func (s *Source) LoadUint(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeUnsigned((*uint)(dst), encoded) {
		Failf("wire value has incompatible uint destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadUint8 loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type uint8.
//   - The storage remains live through this call.
func (s *Source) LoadUint8(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeUnsigned((*uint8)(dst), encoded) {
		Failf("wire value has incompatible uint8 destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadUint16 loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type uint16.
//   - The storage remains live through this call.
func (s *Source) LoadUint16(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeUnsigned((*uint16)(dst), encoded) {
		Failf("wire value has incompatible uint16 destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadUint32 loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type uint32.
//   - The storage remains live through this call.
func (s *Source) LoadUint32(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeUnsigned((*uint32)(dst), encoded) {
		Failf("wire value has incompatible uint32 destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadUint64 loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type uint64.
//   - The storage remains live through this call.
func (s *Source) LoadUint64(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeUnsigned((*uint64)(dst), encoded) {
		Failf("wire value has incompatible uint64 destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadUintptr loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type uintptr.
//   - The storage remains live through this call.
func (s *Source) LoadUintptr(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeUnsigned((*uintptr)(dst), encoded) {
		Failf("wire value has incompatible uintptr destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadFloat32 loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type float32.
//   - The storage remains live through this call.
func (s *Source) LoadFloat32(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeFloat((*float32)(dst), encoded) {
		Failf("wire value has incompatible float32 destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadFloat64 loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type float64.
//   - The storage remains live through this call.
func (s *Source) LoadFloat64(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeFloat((*float64)(dst), encoded) {
		Failf("wire value has incompatible float64 destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadComplex64 loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type complex64.
//   - The storage remains live through this call.
func (s *Source) LoadComplex64(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeComplex((*complex64)(dst), encoded) {
		Failf("wire value has incompatible complex64 destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadComplex128 loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type complex128.
//   - The storage remains live through this call.
func (s *Source) LoadComplex128(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeComplex((*complex128)(dst), encoded) {
		Failf("wire value has incompatible complex128 destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadString loads a checked generated field and optionally waits for dependencies.
// Source and dst are not retained.
//
// Preconditions:
//   - dst points to writable Go storage with underlying type string.
//   - The storage remains live through this call.
func (s *Source) LoadString(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) && !storeString((*string)(dst), encoded) {
		Failf("wire value has incompatible string destination")
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadPointer loads a checked generated pointer field. Source and dst are not
// retained; the graph owns the pointee.
//
// Preconditions:
//   - dst points to writable Go storage of exactly typ, a pointer type.
//   - The storage remains live through this call.
func (s *Source) LoadPointer(slot int, dst unsafe.Pointer, typ reflect.Type, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) {
		ref, ok := encoded.(*wire.Ref)
		if !ok || typ.Kind() != reflect.Ptr {
			Failf("wire value has incompatible pointer destination %v", typ)
		}
		s.internal.ds.assignPointer(dst, typ, ref)
	}
	s.internal.finishField(encoded, wait, nil)
}

// LoadMap loads a checked generated map field. Source is not retained. Entry
// hooks may retain their separately allocated cells.
//
// Preconditions:
//   - dst points to writable Go storage of exactly typ, a map type.
//   - The storage remains live through this call.
func (s *Source) LoadMap(slot int, dst unsafe.Pointer, typ reflect.Type, wait bool) {
	encoded := s.internal.field(slot)
	if !isNilObject(encoded) {
		if typ.Kind() != reflect.Map {
			Failf("wire value has incompatible map destination %v", typ)
		}
		switch x := encoded.(type) {
		case *wire.Ref:
			target := s.internal.ds.mapTarget(x, typ)
			assignMap(dst, typ, target)
		case *wire.Map:
			s.internal.ds.decodeMap(s.internal.ods, reflect.NewAt(typ, dst).Elem(), x)
		default:
			Failf("wire value has incompatible map destination %v", typ)
		}
	}
	s.internal.finishField(encoded, wait, nil)
}
