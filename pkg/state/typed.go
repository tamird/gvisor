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

import "math"

// The methods below save and load fields of predeclared types, which
// go_stateify can identify. They are equivalent to Sink.Save and Source.Load,
// but avoid finding the type of the field at run time. Values of these types
// are never reused from encodings of containing objects (see trySplice).

func b2u8(b bool) uint8 {
	if b {
		return 1
	}
	return 0
}

// SaveBool saves a bool field. See Save.
func (s Sink) SaveBool(slot int, v *bool) {
	es := s.internal.es
	es.out.byte(b2u8(*v))
}

// LoadBool loads a bool field. See Load.
func (s Source) LoadBool(slot int, v *bool) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.byte(); x != 0 || !ds.keepZero {
		*v = x != 0
	}
}

// SaveInt saves a int field. See Save.
func (s Sink) SaveInt(slot int, v *int) {
	es := s.internal.es
	es.out.varint(int64(*v))
}

// LoadInt loads a int field. See Load.
func (s Source) LoadInt(slot int, v *int) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.varint(); x != 0 || !ds.keepZero {
		*v = int(x)
	}
}

// SaveInt8 saves a int8 field. See Save.
func (s Sink) SaveInt8(slot int, v *int8) {
	es := s.internal.es
	es.out.byte(uint8(*v))
}

// LoadInt8 loads a int8 field. See Load.
func (s Source) LoadInt8(slot int, v *int8) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.byte(); x != 0 || !ds.keepZero {
		*v = int8(x)
	}
}

// SaveInt16 saves a int16 field. See Save.
func (s Sink) SaveInt16(slot int, v *int16) {
	es := s.internal.es
	es.out.varint(int64(*v))
}

// LoadInt16 loads a int16 field. See Load.
func (s Source) LoadInt16(slot int, v *int16) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.varint(); x != 0 || !ds.keepZero {
		*v = int16(x)
	}
}

// SaveInt32 saves a int32 field. See Save.
func (s Sink) SaveInt32(slot int, v *int32) {
	es := s.internal.es
	es.out.varint(int64(*v))
}

// LoadInt32 loads a int32 field. See Load.
func (s Source) LoadInt32(slot int, v *int32) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.varint(); x != 0 || !ds.keepZero {
		*v = int32(x)
	}
}

// SaveInt64 saves a int64 field. See Save.
func (s Sink) SaveInt64(slot int, v *int64) {
	es := s.internal.es
	es.out.varint(*v)
}

// LoadInt64 loads a int64 field. See Load.
func (s Source) LoadInt64(slot int, v *int64) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.varint(); x != 0 || !ds.keepZero {
		*v = x
	}
}

// SaveUint saves a uint field. See Save.
func (s Sink) SaveUint(slot int, v *uint) {
	es := s.internal.es
	es.out.uvarint(uint64(*v))
}

// LoadUint loads a uint field. See Load.
func (s Source) LoadUint(slot int, v *uint) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.uvarint(); x != 0 || !ds.keepZero {
		*v = uint(x)
	}
}

// SaveUint8 saves a uint8 field. See Save.
func (s Sink) SaveUint8(slot int, v *uint8) {
	es := s.internal.es
	es.out.byte(*v)
}

// LoadUint8 loads a uint8 field. See Load.
func (s Source) LoadUint8(slot int, v *uint8) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.byte(); x != 0 || !ds.keepZero {
		*v = x
	}
}

// SaveUint16 saves a uint16 field. See Save.
func (s Sink) SaveUint16(slot int, v *uint16) {
	es := s.internal.es
	es.out.uvarint(uint64(*v))
}

// LoadUint16 loads a uint16 field. See Load.
func (s Source) LoadUint16(slot int, v *uint16) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.uvarint(); x != 0 || !ds.keepZero {
		*v = uint16(x)
	}
}

// SaveUint32 saves a uint32 field. See Save.
func (s Sink) SaveUint32(slot int, v *uint32) {
	es := s.internal.es
	es.out.uvarint(uint64(*v))
}

// LoadUint32 loads a uint32 field. See Load.
func (s Source) LoadUint32(slot int, v *uint32) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.uvarint(); x != 0 || !ds.keepZero {
		*v = uint32(x)
	}
}

// SaveUint64 saves a uint64 field. See Save.
func (s Sink) SaveUint64(slot int, v *uint64) {
	es := s.internal.es
	es.out.uvarint(*v)
}

// LoadUint64 loads a uint64 field. See Load.
func (s Source) LoadUint64(slot int, v *uint64) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.uvarint(); x != 0 || !ds.keepZero {
		*v = x
	}
}

// SaveUintptr saves a uintptr field. See Save.
func (s Sink) SaveUintptr(slot int, v *uintptr) {
	es := s.internal.es
	es.out.uvarint(uint64(*v))
}

// LoadUintptr loads a uintptr field. See Load.
func (s Source) LoadUintptr(slot int, v *uintptr) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.uvarint(); x != 0 || !ds.keepZero {
		*v = uintptr(x)
	}
}

// SaveFloat32 saves a float32 field. See Save.
func (s Sink) SaveFloat32(slot int, v *float32) {
	es := s.internal.es
	es.out.uint32(math.Float32bits(*v))
}

// LoadFloat32 loads a float32 field. See Load.
func (s Source) LoadFloat32(slot int, v *float32) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.uint32(); x != 0 || !ds.keepZero {
		*v = math.Float32frombits(x)
	}
}

// SaveFloat64 saves a float64 field. See Save.
func (s Sink) SaveFloat64(slot int, v *float64) {
	es := s.internal.es
	es.out.uint64(math.Float64bits(*v))
}

// LoadFloat64 loads a float64 field. See Load.
func (s Source) LoadFloat64(slot int, v *float64) {
	ds := s.internal.ds
	d := &ds.in
	if x := d.uint64(); x != 0 || !ds.keepZero {
		*v = math.Float64frombits(x)
	}
}

// SaveString saves a string field. See Save.
func (s Sink) SaveString(slot int, v *string) {
	es := s.internal.es
	es.out.string(*v)
}

// LoadString loads a string field. See Load.
func (s Source) LoadString(slot int, v *string) {
	ds := s.internal.ds
	d := &ds.in
	if n := d.byteLength(); n != 0 {
		*v = ds.makeString(d.next(n))
	} else if !ds.keepZero {
		*v = ""
	}
}
