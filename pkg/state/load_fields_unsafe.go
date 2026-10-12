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
)

// LoadBool loads a checked generated field. dst must point to writable Go
// storage whose underlying type is bool, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadBool(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeBool((*bool)(dst), encoded) {
		Failf("wire value has incompatible bool destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadInt loads a checked generated field. dst must point to writable Go
// storage whose underlying type is int, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadInt(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeSigned((*int)(dst), encoded) {
		Failf("wire value has incompatible int destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadInt8 loads a checked generated field. dst must point to writable Go
// storage whose underlying type is int8, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadInt8(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeSigned((*int8)(dst), encoded) {
		Failf("wire value has incompatible int8 destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadInt16 loads a checked generated field. dst must point to writable Go
// storage whose underlying type is int16, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadInt16(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeSigned((*int16)(dst), encoded) {
		Failf("wire value has incompatible int16 destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadInt32 loads a checked generated field. dst must point to writable Go
// storage whose underlying type is int32, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadInt32(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeSigned((*int32)(dst), encoded) {
		Failf("wire value has incompatible int32 destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadInt64 loads a checked generated field. dst must point to writable Go
// storage whose underlying type is int64, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadInt64(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeSigned((*int64)(dst), encoded) {
		Failf("wire value has incompatible int64 destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadUint loads a checked generated field. dst must point to writable Go
// storage whose underlying type is uint, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadUint(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeUnsigned((*uint)(dst), encoded) {
		Failf("wire value has incompatible uint destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadUint8 loads a checked generated field. dst must point to writable Go
// storage whose underlying type is uint8, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadUint8(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeUnsigned((*uint8)(dst), encoded) {
		Failf("wire value has incompatible uint8 destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadUint16 loads a checked generated field. dst must point to writable Go
// storage whose underlying type is uint16, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadUint16(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeUnsigned((*uint16)(dst), encoded) {
		Failf("wire value has incompatible uint16 destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadUint32 loads a checked generated field. dst must point to writable Go
// storage whose underlying type is uint32, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadUint32(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeUnsigned((*uint32)(dst), encoded) {
		Failf("wire value has incompatible uint32 destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadUint64 loads a checked generated field. dst must point to writable Go
// storage whose underlying type is uint64, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadUint64(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeUnsigned((*uint64)(dst), encoded) {
		Failf("wire value has incompatible uint64 destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadUintptr loads a checked generated field. dst must point to writable Go
// storage whose underlying type is uintptr, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadUintptr(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeUnsigned((*uintptr)(dst), encoded) {
		Failf("wire value has incompatible uintptr destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadFloat32 loads a checked generated field. dst must point to writable Go
// storage whose underlying type is float32, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadFloat32(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeFloat((*float32)(dst), encoded) {
		Failf("wire value has incompatible float32 destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadFloat64 loads a checked generated field. dst must point to writable Go
// storage whose underlying type is float64, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadFloat64(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeFloat((*float64)(dst), encoded) {
		Failf("wire value has incompatible float64 destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadComplex64 loads a checked generated field. dst must point to writable Go
// storage whose underlying type is complex64, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadComplex64(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeComplex((*complex64)(dst), encoded) {
		Failf("wire value has incompatible complex64 destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadComplex128 loads a checked generated field. dst must point to writable Go
// storage whose underlying type is complex128, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadComplex128(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeComplex((*complex128)(dst), encoded) {
		Failf("wire value has incompatible complex128 destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadString loads a checked generated field. dst must point to writable Go
// storage whose underlying type is string, and must remain live through this
// call. Source and dst are not retained. wait requests the usual dependency.
func (s *Source) LoadString(slot int, dst unsafe.Pointer, wait bool) {
	encoded := s.internal.field(slot)
	if !encoded.IsNil() && !storeString((*string)(dst), encoded) {
		Failf("wire value has incompatible string destination")
	}
	s.internal.finishField(0, wait, nil)
}

// LoadPointer loads a checked generated pointer field through the common
// resolver. dst must point to writable storage of exactly typ, which must be
// a Go pointer type. The address and Source are not retained.
func (s *Source) LoadPointer(slot int, dst unsafe.Pointer, typ reflect.Type, wait bool) {
	encoded := s.internal.field(slot)
	var root objectID
	if !encoded.IsNil() {
		if typ.Kind() != reflect.Ptr {
			Failf("wire value has incompatible pointer destination %v", typ)
		}
		var target location
		target, root = s.internal.ds.pointerTarget(encoded, typ.Elem(), nil)
		*(*unsafe.Pointer)(dst) = target.address
	}
	s.internal.finishField(root, wait, nil)
}

// LoadObject loads a checked generated aggregate or dynamic field through the
// shared dispatcher. dst must be writable Go storage of exactly typ and remain
// live through this call. Source is not retained; hooks may retain dst.
func (s *Source) LoadObject(slot int, dst unsafe.Pointer, typ reflect.Type, wait bool) {
	encoded := s.internal.field(slot)
	var root objectID
	if !encoded.IsNil() {
		obj := location{ops: operationsFor(typ), address: dst}
		root = s.internal.ds.decodeObject(s.internal.ods, obj, encoded)
	}
	s.internal.finishField(root, wait, nil)
}
