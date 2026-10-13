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

// Allocate creates zero storage for the exact registered graph object type.
// The returned pointer is published before any deferred body is restored.
type Allocate func(reflect.Type) any

// NewObject allocates the exact T represented by its registration.
func NewObject[T any](_ reflect.Type) any { return new(T) }

// LoadField describes an ordinary generated field. Index is its physical Go
// field index; Slot is its stable local schema slot, before wire reconciliation.
type LoadField struct {
	Index int
	Slot  int
	Wait  bool
}

// fieldLoader supplies metadata without relying on package initialization.
// Generated accessors do not dereference their receiver.
type fieldLoader interface {
	Type
	StateLoadFields() []LoadField
}

type restoreField struct {
	offset uintptr
	plan   *restorePlan
	slot   int
	wait   bool
}

// restorePlan contains metadata derived from one actual Go type during Load.
// Child plans and ordinary fields are resolved only when they are used.
type restorePlan struct {
	typ         reflect.Type
	kind        reflect.Kind
	size        uintptr
	element     *restorePlan
	fields      []restoreField
	fieldsReady bool
}

func (ds *decodeState) plan(typ reflect.Type) *restorePlan {
	if plan := ds.types.plans[typ]; plan != nil {
		return plan
	}
	if ds.types.plans == nil {
		ds.types.plans = make(map[reflect.Type]*restorePlan)
	}
	plan := &restorePlan{typ: typ, kind: typ.Kind(), size: typ.Size()}
	ds.types.plans[typ] = plan
	return plan
}

func (ds *decodeState) elementPlan(plan *restorePlan) *restorePlan {
	if plan.element == nil {
		plan.element = ds.plan(plan.typ.Elem())
	}
	return plan.element
}

func restoreType(typ reflect.Type) (registeredType, bool) {
	if info, ok := reverseTypeDatabase[typ]; ok {
		if registered, ok := globalTypeDatabase[info.Name]; ok && registered.typ == typ {
			return registered, true
		}
	}
	if registered, ok := primitiveTypeDatabase[typ.Name()]; ok && registered.typ == typ {
		return registered, true
	}
	return registeredType{}, false
}

func allocateObject(typ reflect.Type) any {
	if registered, ok := restoreType(typ); ok && registered.allocate != nil {
		return registered.allocate(typ)
	}
	return reflect.New(typ).Interface()
}

// LoadFields restores generated ordinary fields in declaration order. Custom
// LoadValue calls and AfterLoad remain in the generated StateLoad method.
// Reconciliation belongs to this Source, not to the receiver's cached layout:
// an identtype loader may forward to a different concrete receiver type.
func (s Source) LoadFields(receiver fieldLoader) {
	pointer := reflect.ValueOf(receiver)
	plan := s.internal.ds.plan(pointer.Type().Elem())
	if !plan.fieldsReady {
		var fields []LoadField
		if registered, ok := restoreType(plan.typ); ok && registered.fields != nil {
			fields = registered.fields
		} else {
			fields = receiver.StateLoadFields()
		}
		// Validate physical metadata against this receiver's local schema.
		// The Source may belong to an identtype that forwarded to it.
		info, ok := reverseTypeDatabase[plan.typ]
		if !ok {
			info.Fields = receiver.StateFields()
		}
		plan.fields = make([]restoreField, 0, len(fields))
		for _, field := range fields {
			member := plan.typ.Field(field.Index)
			if field.Slot < 0 || field.Slot >= len(info.Fields) || member.Name != info.Fields[field.Slot] {
				Failf("invalid generated field %v for %v", field, plan.typ)
			}
			plan.fields = append(plan.fields, restoreField{offset: member.Offset, plan: s.internal.ds.plan(member.Type), slot: field.Slot, wait: field.Wait})
		}
		plan.fieldsReady = true
	}
	if len(plan.fields) == 0 {
		return
	}
	// The old generated &receiver.Field expression requires a nonnil receiver
	// even when the field's wire value is a no-op.
	if pointer.IsNil() {
		Failf("cannot restore fields through nil %T", receiver)
	}
	object := s.internal.ds.pointerDestination(pointer)
	for _, field := range plan.fields {
		encoded := s.internal.field(field.slot)
		target := object.field(field.plan, field.offset)
		s.internal.ds.restoreValue(s.internal.ods, target, encoded)
		if field.wait {
			s.internal.ds.waitObject(s.internal.ods, encoded, nil)
		}
	}
}

// dataValue preserves nil-interface delegation before any address or view is
// required. Wait registration still receives the original parsed value.
func dataValue(value wire.Value) wire.Value {
	for value.Kind() == wire.KindInterface {
		typ, inner := value.Interface()
		if _, ok := typ.(wire.TypeSpecNil); !ok {
			break
		}
		value = inner
	}
	return value
}

// restoreValue owns every encoded-kind operation, for generated fields,
// manual Source calls, graph objects and dynamic interface/map storage alike.
func (ds *decodeState) restoreValue(ods *objectDecodeState, target restoreDestination, encoded wire.Value) {
	encoded = dataValue(encoded)
	switch encoded.Kind() {
	case wire.KindNil:
		return
	case wire.KindRef:
		ds.restoreReference(target, encoded.Reference())
	case wire.KindBool:
		target.storeBool(encoded.Bool())
	case wire.KindInt:
		value := encoded.Int()
		checkInt(value, target.storeInt(value))
	case wire.KindUint:
		value := encoded.Uint()
		checkUint(value, target.storeUint(value))
	case wire.KindFloat32:
		target.storeFloat(encoded.Float())
	case wire.KindFloat64:
		value := encoded.Float()
		checkFloat(value, target.storeFloat(value))
	case wire.KindComplex64:
		target.storeComplex(encoded.Complex())
	case wire.KindComplex128:
		value := encoded.Complex()
		checkComplex(value, target.storeComplex(value))
	case wire.KindString:
		target.storeString(encoded.Text())
	case wire.KindSlice:
		if objectID(encoded.Slice().Ref.Root) != 0 {
			ds.assignSlice(target.value(), encoded.Slice())
		}
	case wire.KindArray:
		ds.restoreArray(ods, target, encoded)
	case wire.KindMap:
		ds.restoreMap(ods, target, encoded)
	case wire.KindStruct:
		ds.decodeStruct(ods, target.value(), encoded)
	case wire.KindInterface:
		ds.decodeInterface(ods, target.value(), encoded)
	default:
		Failf("unknown object %#v for %q", encoded, target.value().Type().Name())
	}
}

// restoreReference retains register/Dots/assignment order. Only ordinary
// pointer stores bypass reflect.Set; maps retain actual source flags and type.
func (ds *decodeState) restoreReference(target restoreDestination, ref wire.Ref) {
	if objectID(ref.Root) == 0 {
		return
	}
	if target.plan == nil {
		target.value().Type() // Preserve the invalid destination error.
	}
	if target.plan.kind == reflect.Map {
		object := walkChild(ref.Dots, reflect.ValueOf(ds.register(&ref, target.plan.typ)).Elem())
		assignMap(target.value(), object)
		return
	}
	pointer := reflect.ValueOf(ds.register(&ref, target.plan.typ.Elem()))
	if len(ref.Dots) != 0 {
		pointer = reflectValueRWAddr(walkChild(ref.Dots, pointer.Elem()))
	}
	if pointer.IsNil() {
		Failf("reference %d has no object storage", ref.Root)
	}
	target.storePointer(pointer)
}

func checkInt(value, stored int64) {
	if stored != value {
		Failf("signed integer truncated from %v to %v", value, stored)
	}
}

func checkUint(value, stored uint64) {
	if stored != value {
		Failf("unsigned integer truncated from %v to %v", value, stored)
	}
}

func checkFloat(value, stored float64) {
	if !isFloatEq(stored, value) {
		Failf("floating point number truncated from %v to %v", value, stored)
	}
}

func checkComplex(value, stored complex128) {
	if !isComplexEq(stored, value) {
		Failf("complex number truncated from %v to %v", value, stored)
	}
}
