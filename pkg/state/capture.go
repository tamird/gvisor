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

// CaptureFields returns shared, owned slots for a generated saver. A manual
// saver may forward a prefix to it, so leave all other slots untouched. Check
// the count once before capturing custom values or indexing a singleton Struct.
func CaptureFields(s Sink, count int) *wire.Struct {
	fields := s.internal.encoded
	if count < 0 || count > fields.Fields() {
		Failf("generated field count %d exceeds struct's %d slots", count, fields.Fields())
	}
	return fields
}

// Capture saves a graph-bearing child into a stable field of the struct. This
// uses the same resolver, deferred queue and object IDs as Sink.Save.
func (s Sink) Capture(objPtr any, dest *wire.Object) {
	s.internal.es.encodeObject(reflect.ValueOf(objPtr).Elem(), encodeDefault, dest)
}

// CaptureValue captures a custom value at the same point as Sink.SaveValue.
func (s Sink) CaptureValue(value any, dest *wire.Object) {
	s.internal.es.encodeObject(reflect.ValueOf(value), encodeDefault, dest)
}

// CapturePointer captures a non-nil pointer in an owned reference cell. The
// generated caller checks nil before converting its typed pointer to any.
// The resolver retains the cell if a containing object is discovered later.
func CapturePointer(s Sink, value any, dest *wire.Object) {
	ref := new(wire.Ref)
	*dest = ref
	s.internal.es.resolve(reflect.ValueOf(value), ref)
}

// CaptureSlice snapshots the slice header and resolves its backing array. The
// existing array owner still clears and captures the unused capacity.
func CaptureSlice(s Sink, value any, dest *wire.Object) {
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
func CaptureMap(s Sink, valuePtr any, dest *wire.Object) {
	s.captureMap(reflect.ValueOf(valuePtr).Elem(), dest)
}

// CaptureMapValue captures a custom map value as Sink.SaveValue does.
func CaptureMapValue(s Sink, value any, dest *wire.Object) {
	s.captureMap(reflect.ValueOf(value), dest)
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
func CaptureInterface(s Sink, valuePtr any, dest *wire.Object) {
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
