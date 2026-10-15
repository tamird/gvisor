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

// Allocate creates zero storage for the exact registered graph object type.
// The returned pointer is published before any deferred body is restored.
type Allocate func(reflect.Type) any

// NewObject allocates the exact T represented by its registration.
func NewObject[T any](_ reflect.Type) any { return new(T) }

// LoadMode describes the exact StateLoad implementation emitted by stateify.
// Handwritten, promoted and forwarding methods must use LoadCustom.
type LoadMode uint8

const (
	// LoadCustom receives an owned Source that it may retain.
	LoadCustom LoadMode = iota
	// LoadGenerated is an exact native generated method. Its custom callbacks
	// capture typed storage, receiver and context, never Source itself.
	LoadGenerated
)

// restorePlan describes one actual Go type. Registered types own their plans;
// other types are scoped to a Load.
type restorePlan struct {
	typ  reflect.Type
	kind reflect.Kind
	size uintptr
}

func newRestorePlan(typ reflect.Type) restorePlan {
	return restorePlan{typ: typ, kind: typ.Kind(), size: typ.Size()}
}

func (ds *decodeState) plan(typ reflect.Type) *restorePlan {
	if registered, ok := restoreType(typ); ok {
		return &registered.restorePlan
	}
	return ds.localPlan(typ)
}

// localPlan owns unregistered and runtime-length types for this Load.
func (ds *decodeState) localPlan(typ reflect.Type) *restorePlan {
	if plan := ds.types.plans[typ]; plan != nil {
		return plan
	}
	if ds.types.plans == nil {
		ds.types.plans = make(map[reflect.Type]*restorePlan)
	}
	plan := newRestorePlan(typ)
	ds.types.plans[typ] = &plan
	return &plan
}

func (ds *decodeState) elementPlan(plan *restorePlan) *restorePlan {
	return ds.plan(plan.typ.Elem())
}

func restoreType(typ reflect.Type) (*registeredType, bool) {
	if entry, ok := reverseTypeDatabase[typ]; ok {
		if registered, ok := globalTypeDatabase[entry.info.Name]; ok && registered.typ == typ {
			return registered, true
		}
	}
	if registered, ok := primitiveTypeDatabase[typ.Name()]; ok && registered.typ == typ {
		return registered, true
	}
	return nil, false
}

func allocateObject(typ reflect.Type) any {
	if registered, ok := restoreType(typ); ok && registered.allocate != nil {
		return registered.allocate(typ)
	}
	return reflect.New(typ).Interface()
}

// LoadInto restores a generated field through its actual Go pointer type.
// Slot reconciliation and wait ordering belong to the Source; the containing
// receiver's type may differ when a manual or identtype loader forwards it.
func LoadInto[T any](s Source, slot int, ptr *T, wait bool) {
	encoded := s.internal.field(slot)
	ds := s.internal.ds
	target := restoreDestination{plan: ds.plan(reflect.TypeFor[T]()), pointer: unsafe.Pointer(ptr)}
	ds.restoreValue(s.internal.ods, target, encoded)
	if wait {
		ds.waitObject(s.internal.ods, encoded, nil)
	}
}

// dataValue preserves nil-interface delegation before any address or view is
// required. Wait registration still receives the original parsed value.
func dataValue(value wire.Value) wire.Value {
	for value.Kind() == wire.KindInterface {
		typ, inner := value.InterfaceView()
		if typ.Kind() != wire.TypeNilKind {
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
		ds.restoreReference(target, encoded.ReferenceView())
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
		if slice := encoded.SliceView(); objectID(slice.Ref.Root()) != 0 {
			ds.assignSlice(target.value(), slice)
		}
	case wire.KindArray:
		ds.restoreArray(ods, target, encoded)
	case wire.KindMap:
		ds.restoreMap(ods, target, encoded)
	case wire.KindStruct:
		ds.decodeStruct(ods, target, encoded)
	case wire.KindInterface:
		ds.decodeInterface(ods, target.value(), encoded)
	default:
		Failf("unknown object %#v for %q", encoded, target.value().Type().Name())
	}
}

// restoreReference retains register/Dots/assignment order. Only ordinary
// pointer stores bypass reflect.Set; maps retain actual source flags and type.
func (ds *decodeState) restoreReference(target restoreDestination, ref wire.ReferenceView) {
	if objectID(ref.Root()) == 0 {
		return
	}
	if target.plan == nil {
		target.value().Type() // Preserve the invalid destination error.
	}
	if target.plan.kind == reflect.Map {
		object := walkChild(ref, reflect.ValueOf(ds.register(ref, target.plan.typ)).Elem())
		assignMap(target.value(), object)
		return
	}
	pointer := reflect.ValueOf(ds.register(ref, target.plan.typ.Elem()))
	if ref.Dots() != 0 {
		pointer = reflectValueRWAddr(walkChild(ref, pointer.Elem()))
	}
	if pointer.IsNil() {
		Failf("reference %d has no object storage", ref.Root())
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
