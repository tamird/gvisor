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

// FieldCodec holds the save/load operations for a generated field. The Go
// compiler supplies the field's actual type, including imported and defined
// types; stateify does not need to resolve source-level type names.
type FieldCodec[T any] struct {
	save func(Sink, int, *T)
	load func(Source, int, *T)
}

// NewFieldCodec selects operations once, when the generated package initializes.
// The pointer is used only to infer T and is not retained or dereferenced.
func NewFieldCodec[T any](_ *T) FieldCodec[T] {
	switch reflect.TypeFor[T]().Kind() {
	case reflect.Bool:
		return scalarFieldCodec[T](saveBool[bool], loadBool[bool])
	case reflect.Int:
		return scalarFieldCodec[T](saveSigned[int], loadSigned[int])
	case reflect.Int8:
		return scalarFieldCodec[T](saveSigned[int8], loadSigned[int8])
	case reflect.Int16:
		return scalarFieldCodec[T](saveSigned[int16], loadSigned[int16])
	case reflect.Int32:
		return scalarFieldCodec[T](saveSigned[int32], loadSigned[int32])
	case reflect.Int64:
		return scalarFieldCodec[T](saveSigned[int64], loadSigned[int64])
	case reflect.Uint:
		return scalarFieldCodec[T](saveUnsigned[uint], loadUnsigned[uint])
	case reflect.Uint8:
		return scalarFieldCodec[T](saveUnsigned[uint8], loadUnsigned[uint8])
	case reflect.Uint16:
		return scalarFieldCodec[T](saveUnsigned[uint16], loadUnsigned[uint16])
	case reflect.Uint32:
		return scalarFieldCodec[T](saveUnsigned[uint32], loadUnsigned[uint32])
	case reflect.Uint64:
		return scalarFieldCodec[T](saveUnsigned[uint64], loadUnsigned[uint64])
	case reflect.Uintptr:
		return scalarFieldCodec[T](saveUnsigned[uintptr], loadUnsigned[uintptr])
	case reflect.String:
		return scalarFieldCodec[T](saveString[string], loadString[string])
	case reflect.Float32:
		return scalarFieldCodec[T](saveFloat[float32], loadFloat[float32])
	case reflect.Float64:
		return scalarFieldCodec[T](saveFloat[float64], loadFloat[float64])
	case reflect.Complex64:
		return scalarFieldCodec[T](saveComplex[complex64], loadComplex[complex64])
	case reflect.Complex128:
		return scalarFieldCodec[T](saveComplex[complex128], loadComplex[complex128])
	default:
		return FieldCodec[T]{
			save: func(s Sink, slot int, value *T) { s.Save(slot, value) },
			load: func(s Source, slot int, value *T) { s.Load(slot, value) },
		}
	}
}

// scalarFieldCodec is called only after NewFieldCodec establishes that T and U
// have the same underlying scalar representation. This includes defined types,
// which would be missed by a type switch over the value. Pointer conversion is
// confined here; graph objects continue to use the shared resolver.
func scalarFieldCodec[T, U any](save func(Sink, int, *U), load func(*U, wire.Object) bool) FieldCodec[T] {
	return FieldCodec[T]{
		save: func(s Sink, slot int, value *T) { save(s, slot, (*U)(unsafe.Pointer(value))) },
		load: func(s Source, slot int, value *T) {
			encoded := s.field(slot)
			if !load((*U)(unsafe.Pointer(value)), encoded) {
				// The dynamic path must retain T: interface assignment checks
				// depend on the field's actual type, not just its representation.
				loadTypedFallback(s, value, encoded)
			}
		},
	}
}

// Save saves the field with the operations selected for its declared type.
func (c FieldCodec[T]) Save(s Sink, slot int, value *T) { c.save(s, slot, value) }

// Load loads a field after reconciling checkpoint field names with local slots.
func (c FieldCodec[T]) Load(s Source, slot int, value *T) { c.load(s, slot, value) }

// LoadWait also preserves the field's dependency on referred objects' hooks.
func (c FieldCodec[T]) LoadWait(s Source, slot int, value *T) {
	c.load(s, slot, value)
	s.internal.ds.waitObject(s.internal.ods, s.field(slot), nil)
}

type signed interface {
	~int | ~int8 | ~int16 | ~int32 | ~int64
}

type unsigned interface {
	~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr
}

type floating interface {
	~float32 | ~float64
}

type complexNumber interface {
	~complex64 | ~complex128
}

func (s Sink) field(slot int) *wire.Object {
	return s.internal.encoded.Field(slot)
}

func (s Source) field(slot int) wire.Object {
	return *s.internal.encoded.Field(s.internal.rte.FieldOrder[slot])
}

func loadTypedFallback[T any](s Source, value *T, encoded wire.Object) {
	s.internal.ds.decodeObject(s.internal.ods, reflect.ValueOf(value).Elem(), encoded)
}

// saveSigned saves a generated signed integer field, including defined types.
func saveSigned[T signed](s Sink, slot int, value *T) {
	if *value == 0 {
		*s.field(slot) = wire.Nil{}
		return
	}
	*s.field(slot) = wire.Int(*value)
}

// loadSigned loads a generated signed integer field with truncation checking.
func loadSigned[T signed](value *T, encoded wire.Object) bool {
	switch x := encoded.(type) {
	case wire.Nil:
	case wire.Int:
		*value = T(x)
		checkInt(int64(x), int64(*value))
	default:
		return false
	}
	return true
}

// saveUnsigned saves a generated unsigned integer field, including defined types.
func saveUnsigned[T unsigned](s Sink, slot int, value *T) {
	if *value == 0 {
		*s.field(slot) = wire.Nil{}
		return
	}
	*s.field(slot) = wire.Uint(*value)
}

// loadUnsigned loads a generated unsigned integer field with truncation checking.
func loadUnsigned[T unsigned](value *T, encoded wire.Object) bool {
	switch x := encoded.(type) {
	case wire.Nil:
	case wire.Uint:
		*value = T(x)
		checkUint(uint64(x), uint64(*value))
	default:
		return false
	}
	return true
}

// saveBool saves a generated boolean field, including defined types.
func saveBool[T ~bool](s Sink, slot int, value *T) {
	if !*value {
		*s.field(slot) = wire.Nil{}
		return
	}
	*s.field(slot) = wire.Bool(*value)
}

// loadBool loads a generated boolean field.
func loadBool[T ~bool](value *T, encoded wire.Object) bool {
	switch x := encoded.(type) {
	case wire.Nil:
	case wire.Bool:
		*value = T(x)
	default:
		return false
	}
	return true
}

// saveString saves a generated string field, including defined types.
func saveString[T ~string](s Sink, slot int, value *T) {
	if *value == "" {
		*s.field(slot) = wire.Nil{}
		return
	}
	x := wire.String(*value)
	*s.field(slot) = &x
}

// loadString loads a generated string field.
func loadString[T ~string](value *T, encoded wire.Object) bool {
	switch x := encoded.(type) {
	case wire.Nil:
	case *wire.String:
		*value = T(*x)
	default:
		return false
	}
	return true
}

// saveFloat saves a generated floating-point field at its declared width.
func saveFloat[T floating](s Sink, slot int, value *T) {
	if *value == 0 {
		*s.field(slot) = wire.Nil{}
		return
	}
	if unsafe.Sizeof(*value) == 4 {
		*s.field(slot) = wire.Float32(float64(*value))
	} else {
		*s.field(slot) = wire.Float64(*value)
	}
}

// loadFloat loads a generated floating-point field with truncation checking.
func loadFloat[T floating](value *T, encoded wire.Object) bool {
	switch x := encoded.(type) {
	case wire.Nil:
	case wire.Float32:
		*value = T(float64(x))
	case wire.Float64:
		*value = T(x)
		checkFloat(float64(x), float64(*value))
	default:
		return false
	}
	return true
}

// saveComplex saves a generated complex field at its declared width.
func saveComplex[T complexNumber](s Sink, slot int, value *T) {
	if *value == 0 {
		*s.field(slot) = wire.Nil{}
		return
	}
	if unsafe.Sizeof(*value) == 8 {
		x := wire.Complex64(complex128(*value))
		*s.field(slot) = &x
	} else {
		x := wire.Complex128(*value)
		*s.field(slot) = &x
	}
}

// loadComplex loads a generated complex field with truncation checking.
func loadComplex[T complexNumber](value *T, encoded wire.Object) bool {
	switch x := encoded.(type) {
	case wire.Nil:
	case *wire.Complex64:
		*value = T(complex128(*x))
	case *wire.Complex128:
		*value = T(*x)
		checkComplex(complex128(*x), complex128(*value))
	default:
		return false
	}
	return true
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
