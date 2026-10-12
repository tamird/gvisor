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

func (f *boundFields) location(slot int) location {
	if slot < 0 || slot >= len(f.fields) || f.fields[slot].ops == nil {
		Failf("invalid generated field slot %d", slot)
	}
	field := f.fields[slot]
	return location{ops: field.ops, address: unsafe.Add(f.base, field.offset)}
}

// FieldSource addresses the generated loader's actual receiver. Local field
// selection is independent of the incoming wire field reconciliation.
type FieldSource struct {
	boundFields
	decoder objectDecoder
}

// Fields binds the actual generated receiver using checked local field indices.
// Incoming wire order remains the Source's concern.
func (s Source) Fields(receiver any, indices []int) FieldSource {
	return FieldSource{boundFields: bindFields(receiver, indices), decoder: s.internal}
}

// Load decodes a declared field using its already-resolved exact operations.
func (s *FieldSource) Load(slot int) {
	s.decoder.loadLocation(slot, s.location(slot), false, nil)
}

// LoadWait also waits for the field's referenced objects before AfterLoad.
func (s *FieldSource) LoadWait(slot int) {
	s.decoder.loadLocation(slot, s.location(slot), true, nil)
}
