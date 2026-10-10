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

func (s Sink) field(slot int) *wire.Object {
	return s.internal.encoded.Field(slot)
}

func (s Source) field(slot int) wire.Object {
	return *s.internal.encoded.Field(s.internal.rte.FieldOrder[slot])
}

func loadTypedFallback[T any](s Source, value *T, encoded wire.Object) {
	s.internal.ds.decodeObject(s.internal.ods, reflect.ValueOf(value).Elem(), encoded)
}

// SavePointer saves a generated pointer field through the shared object graph.
func SavePointer[T any, P ~*T](s Sink, slot int, value *P) {
	if *value == nil {
		*s.field(slot) = wire.Nil{}
		return
	}
	r := new(wire.Ref)
	*s.field(slot) = r
	s.internal.es.resolve(reflect.ValueOf(*value), r)
}

// LoadPointer loads a generated pointer field through the shared object graph.
// wait preserves the field's state:"wait" dependency on the pointee's hooks.
func LoadPointer[T any, P ~*T](s Source, slot int, value *P, wait bool) {
	encoded := s.field(slot)
	switch x := encoded.(type) {
	case wire.Nil:
	case *wire.Ref:
		if x.Root != 0 {
			v := s.internal.ds.registerWithAllocator(x, reflect.TypeFor[T](), func() reflect.Value {
				return reflect.ValueOf(new(T)).Elem()
			})
			*value = P(typedValueRWAddr[T](v))
		}
	default:
		loadTypedFallback(s, value, encoded)
	}
	if wait {
		s.internal.ds.waitObject(s.internal.ods, encoded, nil)
	}
}
