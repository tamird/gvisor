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

// Record returns the captured fields owned by this generated saver. Usually
// its exact receiver metadata selected a constructor before StateSave. Forwarding
// from a manual saver instead adapts these fields into that saver's slots.
// The saver must capture values now, not retain live fields for emission.
// It must assert the exact capture type so promoted savers cannot use another
// type's layout. newRecord is the same constructor returned by its metadata.
func Record(s Sink, count int, newRecord func(wire.TypeID, int) *wire.Record) wire.RecordFields {
	return wire.CaptureRecordValue(s.internal.encoded, count, newRecord)
}

// ForwardRecord resolves an identtype's capture layout on the actual converted
// receiver. It retains exact registration and promoted-method checks, without
// constructing a closure during package initialization.
func ForwardRecord(owner Type) func(wire.TypeID, int) *wire.Record {
	typ := reflect.TypeOf(owner).Elem()
	name := owner.StateTypeName()
	if registered, _ := registeredType(name); registered != typ {
		Failf("forwarded record type %s is not registered as %v", name, typ)
	}
	if factory := generatedMetadata(owner, name, typ).NewRecord; factory != nil {
		return factory
	}
	return wire.NewSlotRecord
}

// Capture saves a graph-bearing child into a stable field of the record. This
// uses the same resolver, deferred queue and object IDs as Sink.Save.
func (s Sink) Capture(objPtr any, dest *wire.Object) {
	s.internal.es.encodeObject(reflect.ValueOf(objPtr).Elem(), encodeDefault, dest)
}

// CaptureValue captures a custom value at the same point as Sink.SaveValue.
func (s Sink) CaptureValue(value any, dest *wire.Object) {
	s.internal.es.encodeObject(reflect.ValueOf(value), encodeDefault, dest)
}

// CapturePointer records a pointer in a stable, inline reference slot. The
// resolver may change that slot if a containing object is discovered later.
// The slot must initially be zero and must outlive graph discovery.
func CapturePointer[T any](s Sink, value *T, dest *wire.Ref) {
	if value != nil {
		s.internal.es.resolve(reflect.ValueOf(value), dest)
	}
}

// CaptureSlice snapshots the slice header and resolves its backing array. The
// existing array owner still clears and captures the unused capacity.
func CaptureSlice(s Sink, value any, dest *wire.Slice) {
	obj := reflect.ValueOf(value)
	dest.Length = wire.Uint(obj.Len())
	dest.Capacity = wire.Uint(obj.Cap())
	if !obj.IsNil() {
		s.internal.es.resolve(arrayFromSlice(obj), &dest.Ref)
	}
}

// CaptureMap preserves Sink.Save's addressable map value. In particular, a
// later save hook may reassign the original map variable before its deferred
// capture; passing the map by value here would change that existing timing.
func CaptureMap(s Sink, valuePtr any, dest *wire.Ref) {
	s.internal.es.resolve(reflect.ValueOf(valuePtr).Elem(), dest)
}

// CaptureMapValue captures a custom map value as Sink.SaveValue does.
func CaptureMapValue(s Sink, value any, dest *wire.Ref) {
	s.internal.es.resolve(reflect.ValueOf(value), dest)
}

// CaptureInterface saves an interface's dynamic type and value. A nil Type in
// dest denotes the primitive-zero field, rather than a typed interface value.
func CaptureInterface(s Sink, valuePtr any, dest *wire.Interface) {
	obj := reflect.ValueOf(valuePtr).Elem()
	if obj.IsNil() {
		return
	}
	value := obj.Elem()
	dest.Type = s.internal.es.findType(value.Type())
	s.internal.es.encodeObject(value, encodeAsValue, &dest.Value)
}
