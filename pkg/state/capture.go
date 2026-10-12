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
// its exact registered constructor allocated them before StateSave. Forwarding
// from a manual saver instead adapts these fields into that saver's slots.
// The saver must capture values now, not retain live fields for emission.
func Record[T any, P interface {
	*T
	wire.RecordFields
}](s Sink, count int) *T {
	return wire.CaptureRecordValue[T, P](s.internal.encoded, count)
}

// ForwardRecord preserves the capture layout of an identtype's declared
// underlying type. Resolution is deferred until saving, after all package
// registrations have completed; init ordering within a package is irrelevant.
func ForwardRecord(owner Type) func(wire.TypeID, int) *wire.Record {
	name := owner.StateTypeName()
	typ := reflect.TypeOf(owner).Elem()
	return func(id wire.TypeID, count int) *wire.Record {
		registered, ok := globalTypeDatabase[name]
		if !ok || registered.typ != typ {
			Failf("forwarded record type %s is not registered as %v", name, typ)
		}
		if registered.newRecord == nil {
			return wire.NewSlotRecord(id, count)
		}
		return registered.newRecord(id, count)
	}
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

// CaptureMap preserves Sink.Save's addressable map value. In particular, a
// later save hook may reassign the original map variable before its deferred
// capture; passing the map by value here would change that existing timing.
func CaptureMap(s Sink, valuePtr any, dest *wire.Ref) {
	s.internal.es.resolveMap(reflect.ValueOf(valuePtr).Elem(), dest)
}

// CaptureMapValue captures a custom map value as Sink.SaveValue does.
func CaptureMapValue(s Sink, value any, dest *wire.Ref) {
	s.internal.es.resolveMap(reflect.ValueOf(value), dest)
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
