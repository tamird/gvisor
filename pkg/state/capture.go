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

// CheckCaptureFields checks the owned slots for a generated saver. A manual
// saver may forward a prefix to it, so leave all other slots untouched. Check
// the count once before capturing custom values or indexing a singleton Struct.
func CheckCaptureFields(s Sink, count int) {
	fields := s.internal.encoded
	if count < 0 || count > fields.Fields() {
		Failf("generated field count %d exceeds struct's %d slots", count, fields.Fields())
	}
}

// CaptureNil overwrites a generated field with the ordinary zero encoding.
func CaptureNil(s Sink, slot int) {
	*s.internal.encoded.Field(slot) = wire.Nil{}
}

// Generated scalar captures own both field selection and the typed store.
// Ordinary zero fields are elided; values nested inside interfaces and arrays
// retain their complete representation through the shared graph encoder.

// CaptureInt snapshots a signed integer field with zero elision.
func CaptureInt(s Sink, slot int, value wire.Int) {
	dest := s.internal.encoded.Field(slot)
	if value == 0 {
		*dest = wire.Nil{}
	} else {
		*dest = value
	}
}

// CaptureString snapshots a string field with zero elision.
func CaptureString(s Sink, slot int, value wire.String) {
	dest := s.internal.encoded.Field(slot)
	if value == "" {
		*dest = wire.Nil{}
	} else {
		*dest = &value
	}
}

// CaptureUint snapshots an unsigned integer field with zero elision.
func CaptureUint(s Sink, slot int, value wire.Uint) {
	dest := s.internal.encoded.Field(slot)
	if value == 0 {
		*dest = wire.Nil{}
	} else {
		*dest = value
	}
}

// CaptureBool snapshots a boolean field with zero elision.
func CaptureBool(s Sink, slot int, value wire.Bool) {
	dest := s.internal.encoded.Field(slot)
	if !value {
		*dest = wire.Nil{}
	} else {
		*dest = value
	}
}

// CaptureFloat32 preserves the promotion and narrowing boundary used by
// reflective capture, including signaling NaNs.
func CaptureFloat32(s Sink, slot int, value wire.Float64) {
	dest := s.internal.encoded.Field(slot)
	if value == 0 {
		*dest = wire.Nil{}
	} else {
		*dest = wire.Float32(value)
	}
}

// CaptureFloat64 snapshots a floating-point field with zero elision.
func CaptureFloat64(s Sink, slot int, value wire.Float64) {
	dest := s.internal.encoded.Field(slot)
	if value == 0 {
		*dest = wire.Nil{}
	} else {
		*dest = value
	}
}

// CaptureComplex64 preserves the promotion and narrowing boundary used by
// reflective capture, including signaling NaNs.
func CaptureComplex64(s Sink, slot int, value wire.Complex128) {
	dest := s.internal.encoded.Field(slot)
	if value == 0 {
		*dest = wire.Nil{}
	} else {
		narrow := wire.Complex64(value)
		*dest = &narrow
	}
}

// CaptureComplex128 snapshots a complex field with zero elision.
func CaptureComplex128(s Sink, slot int, value wire.Complex128) {
	dest := s.internal.encoded.Field(slot)
	if value == 0 {
		*dest = wire.Nil{}
	} else {
		*dest = &value
	}
}

// CapturePointer captures a non-nil pointer in an owned reference cell. The
// generated caller checks nil before converting its typed pointer to any.
// The resolver retains the cell if a containing object is discovered later.
func CapturePointer(s Sink, slot int, value any) {
	dest := s.internal.encoded.Field(slot)
	ref := new(wire.Ref)
	*dest = ref
	s.internal.es.resolve(reflect.ValueOf(value), ref)
}

// CaptureSlice snapshots the slice header and resolves its backing array. The
// existing array owner still clears and captures the unused capacity.
func CaptureSlice(s Sink, slot int, value any) {
	dest := s.internal.encoded.Field(slot)
	obj := reflect.ValueOf(value)
	if obj.IsNil() {
		*dest = wire.Nil{}
		return
	}
	captured := &wire.Slice{Length: wire.Uint(obj.Len()), Capacity: wire.Uint(obj.Cap())}
	*dest = captured
	s.internal.es.resolve(arrayFromSlice(obj), &captured.Ref)
}

// CaptureMap preserves Sink.Save's addressable map value. In particular, a
// later save hook may reassign the original map variable before its deferred
// capture; passing the map by value here would change that existing timing.
func CaptureMap(s Sink, slot int, valuePtr any) {
	s.captureMap(reflect.ValueOf(valuePtr).Elem(), s.internal.encoded.Field(slot))
}

// CaptureMapValue captures a custom map value as Sink.SaveValue does.
func CaptureMapValue(s Sink, slot int, value any) {
	s.captureMap(reflect.ValueOf(value), s.internal.encoded.Field(slot))
}

func (s Sink) captureMap(obj reflect.Value, dest *wire.Object) {
	if obj.IsNil() {
		*dest = wire.Nil{}
		return
	}
	ref := new(wire.Ref)
	*dest = ref
	s.internal.es.resolve(obj, ref)
}

// CaptureInterface saves an interface's dynamic type and value. An ordinary
// nil field is elided; typed nils inside it retain their complete wire form.
func CaptureInterface(s Sink, slot int, valuePtr any) {
	dest := s.internal.encoded.Field(slot)
	obj := reflect.ValueOf(valuePtr).Elem()
	if obj.IsNil() {
		*dest = wire.Nil{}
		return
	}
	value := obj.Elem()
	captured := &wire.Interface{Type: s.internal.es.findType(value.Type())}
	*dest = captured
	s.internal.es.encodeObject(value, encodeAsValue, &captured.Value)
}
