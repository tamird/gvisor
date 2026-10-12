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

// LoadPointer decodes a generated pointer field using the common graph resolver.
// source is not retained; wait requests the ordinary field dependency.
func LoadPointer[P ~*T, T any](source *Source, slot int, dst *P, wait bool) {
	encoded := source.internal.field(slot)
	if !isNilObject(encoded) {
		target := source.internal.ds.pointerTarget(encoded, reflect.TypeFor[T](), nil)
		*dst = P((*T)(target.address))
	}
	source.internal.finishField(encoded, wait, nil)
}

// LoadMap decodes a generated map field. Payloads still use the shared dynamic
// map loop, including fresh cells and immediate insertion after recording dependencies.
func LoadMap[M ~map[K]V, K comparable, V any](source *Source, slot int, dst *M, wait bool) {
	encoded := source.internal.field(slot)
	if !isNilObject(encoded) {
		typ := reflect.TypeFor[M]()
		if ref, ok := encoded.(*wire.Ref); ok {
			target := source.internal.ds.mapTarget(ref, typ, nil)
			if target.ops.layout.typ == typ {
				m := (*M)(target.address)
				if *m == nil {
					*m = make(M)
				}
				*dst = *m
			} else {
				assignMap(reflect.ValueOf(dst).Elem(), target.reflect())
			}
		} else {
			obj := location{ops: operationsFor(typ), address: unsafe.Pointer(dst)}
			obj.ops.decode(source.internal.ds, source.internal.ods, obj, encoded)
		}
	}
	source.internal.finishField(encoded, wait, nil)
}

// LoadSlice resolves and checks the complete backing extent before assigning a
// generated slice field. It does not require a registered type-specific callback.
func LoadSlice[S ~[]E, E any](source *Source, slot int, dst *S, wait bool) {
	encoded := source.internal.field(slot)
	if !isNilObject(encoded) {
		target, length, capacity := source.internal.ds.sliceTarget(encoded, reflect.TypeFor[E]())
		*dst = S(unsafe.Slice((*E)(target.address), capacity)[:length:capacity])
	}
	source.internal.finishField(encoded, wait, nil)
}

// LoadStruct supplies the known receiver pointer to the common hook and binary
// codec dispatch without reconstructing it from a reflected field address.
func LoadStruct[T any](source *Source, slot int, dst *T, wait bool) {
	encoded := source.internal.field(slot)
	if !isNilObject(encoded) {
		source.internal.ds.decodeStructValue(source.internal.ods, reflect.TypeFor[T](), dst, encoded)
	}
	source.internal.finishField(encoded, wait, nil)
}

// LoadObject handles generated arrays, interfaces and unsupported field kinds
// through the same runtime dispatcher used by public/manual Source calls.
func LoadObject[T any](source *Source, slot int, dst *T, wait bool) {
	encoded := source.internal.field(slot)
	if !isNilObject(encoded) {
		obj := location{ops: operationsFor(reflect.TypeFor[T]()), address: unsafe.Pointer(dst)}
		obj.ops.decode(source.internal.ds, source.internal.ods, obj, encoded)
	}
	source.internal.finishField(encoded, wait, nil)
}
