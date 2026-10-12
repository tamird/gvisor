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

type fieldOperation struct {
	offset uintptr
	ops    *typeOperations
}

// operationsForFields resolves the checked generated declaration once per
// exact receiver type. indices is read only during construction and is never
// retained, so generated calls need no initialized descriptor global.
func operationsForFields(pointerType reflect.Type, indices []int) []fieldOperation {
	if cached, ok := operationRegistry.fields.Load(pointerType); ok {
		return cached.([]fieldOperation)
	}
	operationRegistry.Lock()
	defer operationRegistry.Unlock()
	if cached, ok := operationRegistry.fields.Load(pointerType); ok {
		return cached.([]fieldOperation)
	}
	builder := operationBuilder{staged: make(map[reflect.Type]*typeOperations)}
	fields := make([]fieldOperation, len(indices))
	typ := pointerType.Elem()
	for slot, index := range indices {
		if index == -1 {
			continue
		}
		if index < 0 || index >= typ.NumField() {
			Failf("generated field index %d is invalid for %v", index, typ)
		}
		field := typ.Field(index)
		if field.Offset > typ.Size() || field.Type.Size() > typ.Size()-field.Offset {
			Failf("generated field %s is outside %v", field.Name, typ)
		}
		fields[slot] = fieldOperation{offset: field.Offset, ops: builder.forType(field.Type)}
	}
	builder.publish()
	operationRegistry.fields.Store(pointerType, fields)
	return fields
}

type boundFields struct {
	base   unsafe.Pointer
	fields []fieldOperation
}

func bindFields(receiver any, indices []int) boundFields {
	obj := reflect.ValueOf(receiver)
	if !obj.IsValid() || obj.Kind() != reflect.Ptr || obj.IsNil() || obj.Type().Elem().Kind() != reflect.Struct {
		Failf("invalid generated field receiver %T", receiver)
	}
	return boundFields{base: obj.UnsafePointer(), fields: operationsForFields(obj.Type(), indices)}
}

func (f boundFields) value(slot int) value {
	if slot < 0 || slot >= len(f.fields) || f.fields[slot].ops == nil {
		Failf("invalid generated field slot %d", slot)
	}
	field := f.fields[slot]
	return valueAt(unsafe.Add(f.base, field.offset), field.ops)
}

// FieldSink addresses declared fields of this generated callee's receiver.
// The receiver may differ from the object whose manual saver supplied Sink.
type FieldSink struct {
	boundFields
	es *encodeState
}

// Fields binds the actual generated receiver without reading its fields.
// indices maps local wire slots to direct Go field indices, with -1 for custom
// or unsupported fields. Stateify supplies one consistent map per exact type.
func (s Sink) Fields(receiver any, indices []int) FieldSink {
	return FieldSink{boundFields: bindFields(receiver, indices), es: s.internal.es}
}

// Capture captures an ordinary graph-bearing field through the common engine.
func (s FieldSink) Capture(slot int, dest *wire.Object) {
	s.es.encodeValue(s.value(slot), encodeDefault, dest)
}

// CapturePointer resolves the field's current pointed-to object.
func (s FieldSink) CapturePointer(slot int, dest *wire.Ref) {
	value := s.value(slot)
	value.require(reflect.Ptr)
	if pointer := value.pointer(); pointer != nil {
		s.es.resolveValue(valueAt(pointer, value.ops.elem), uintptr(pointer), dest, encodeDefault)
	}
}

// CaptureMap retains the addressed map slot for deferred capture.
func (s FieldSink) CaptureMap(slot int, dest *wire.Ref) {
	s.es.resolveMapValue(s.value(slot), dest)
}

// CaptureSlice snapshots the header and clears unused capacity immediately.
func (s FieldSink) CaptureSlice(slot int, dest *wire.Slice) {
	value := s.value(slot)
	value.require(reflect.Slice)
	value.ops.slice.capture(s.es, value, dest)
}

// CaptureInterface crosses the dynamic interface boundary at capture time.
func (s FieldSink) CaptureInterface(slot int, dest *wire.Interface) {
	value := s.value(slot)
	value.require(reflect.Interface)
	s.es.captureInterface(value.reflect(), dest)
}

// FieldSource addresses the generated loader's actual receiver. Local field
// selection is independent of the incoming wire field reconciliation.
type FieldSource struct {
	boundFields
	decoder objectDecoder
}

// Fields binds the actual generated receiver using the same checked local
// indices as Sink.Fields. Incoming wire order remains the Source's concern.
func (s Source) Fields(receiver any, indices []int) FieldSource {
	return FieldSource{boundFields: bindFields(receiver, indices), decoder: s.internal}
}

// Load decodes a declared field using its already-resolved exact operations.
func (s FieldSource) Load(slot int) {
	s.decoder.loadValue(slot, s.value(slot), false, nil)
}

// LoadWait also waits for the field's referenced objects before AfterLoad.
func (s FieldSource) LoadWait(slot int) {
	s.decoder.loadValue(slot, s.value(slot), true, nil)
}
