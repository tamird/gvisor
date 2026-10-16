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
	fields := len(s.internal.fields)
	if count < 0 || count > fields {
		Failf("generated field count %d exceeds struct's %d slots", count, fields)
	}
}

// CaptureNil overwrites a generated field with the ordinary zero encoding.
//
//go:noinline
func CaptureNil(s Sink, slot int) {
	*s.internal.field(slot) = wire.Nil{}
}

// CapturePointer captures a non-nil pointer in an owned reference cell. The
// generated caller checks nil before converting its typed pointer to any.
// The resolver retains the cell if a containing object is discovered later.
func CapturePointer(s Sink, slot int, value any) {
	dest := s.internal.field(slot)
	ref := new(wire.Ref)
	*dest = ref
	s.internal.es.resolve(reflect.ValueOf(value), ref)
}

// CaptureMap preserves Sink.Save's addressable map value. In particular, a
// later save hook may reassign the original map variable before its deferred
// capture; passing the map by value here would change that existing timing.
func CaptureMap(s Sink, slot int, valuePtr any) {
	s.captureMap(reflect.ValueOf(valuePtr).Elem(), s.internal.field(slot))
}

// CaptureMapValue captures a custom map value as Sink.SaveValue does.
func CaptureMapValue(s Sink, slot int, value any) {
	s.captureMap(reflect.ValueOf(value), s.internal.field(slot))
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
	dest := s.internal.field(slot)
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
