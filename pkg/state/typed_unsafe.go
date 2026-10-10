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
	primitive primitiveCodec
}

// primitiveCodec erases the field type only for the exact primitive wire path.
// Each representation has one shared implementation. Unexpected wire values
// return to the typed facade before dynamic assignment checks are performed.
type primitiveCodec struct {
	save func(Sink, int, unsafe.Pointer)
	load func(unsafe.Pointer, wire.Object) bool
}

// These operations are instantiated once per primitive representation, not for
// every (field type, representation) pair. Selecting the kind before erasing
// the pointer establishes the layout needed by each typed load/store.
var primitiveCodecs = [...]primitiveCodec{
	reflect.Bool:       {save: saveBool[bool], load: loadBool[bool]},
	reflect.Int:        {save: saveSigned[int], load: loadSigned[int]},
	reflect.Int8:       {save: saveSigned[int8], load: loadSigned[int8]},
	reflect.Int16:      {save: saveSigned[int16], load: loadSigned[int16]},
	reflect.Int32:      {save: saveSigned[int32], load: loadSigned[int32]},
	reflect.Int64:      {save: saveSigned[int64], load: loadSigned[int64]},
	reflect.Uint:       {save: saveUnsigned[uint], load: loadUnsigned[uint]},
	reflect.Uint8:      {save: saveUnsigned[uint8], load: loadUnsigned[uint8]},
	reflect.Uint16:     {save: saveUnsigned[uint16], load: loadUnsigned[uint16]},
	reflect.Uint32:     {save: saveUnsigned[uint32], load: loadUnsigned[uint32]},
	reflect.Uint64:     {save: saveUnsigned[uint64], load: loadUnsigned[uint64]},
	reflect.Uintptr:    {save: saveUnsigned[uintptr], load: loadUnsigned[uintptr]},
	reflect.String:     {save: saveString[string], load: loadString[string]},
	reflect.Float32:    {save: saveFloat[float32], load: loadFloat[float32]},
	reflect.Float64:    {save: saveFloat[float64], load: loadFloat[float64]},
	reflect.Complex64:  {save: saveComplex[complex64], load: loadComplex[complex64]},
	reflect.Complex128: {save: saveComplex[complex128], load: loadComplex[complex128]},
}

// NewFieldCodec selects operations once, when the generated package initializes.
// The pointer is used only to infer T and is not retained or dereferenced.
func NewFieldCodec[T any](_ *T) FieldCodec[T] {
	kind := reflect.TypeFor[T]().Kind()
	if int(kind) >= len(primitiveCodecs) {
		return FieldCodec[T]{}
	}
	return FieldCodec[T]{primitive: primitiveCodecs[kind]}
}

// Save saves the field with the operations selected for its declared type.
func (c FieldCodec[T]) Save(s Sink, slot int, value *T) {
	if c.primitive.save == nil {
		s.Save(slot, value)
		return
	}
	c.primitive.save(s, slot, unsafe.Pointer(value))
}

// Load loads a field after reconciling checkpoint field names with local slots.
func (c FieldCodec[T]) Load(s Source, slot int, value *T) {
	encoded := s.field(slot)
	if c.primitive.load == nil || !c.primitive.load(unsafe.Pointer(value), encoded) {
		// Preserve T here: interface assignment depends on the field's
		// actual type, not just its primitive representation.
		loadTypedFallback(s, value, encoded)
	}
}

// LoadWait also preserves the field's dependency on referred objects' hooks.
func (c FieldCodec[T]) LoadWait(s Source, slot int, value *T) {
	c.Load(s, slot, value)
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
func saveSigned[T signed](s Sink, slot int, ptr unsafe.Pointer) {
	value := (*T)(ptr)
	if *value == 0 {
		*s.field(slot) = wire.Nil{}
		return
	}
	*s.field(slot) = wire.Int(*value)
}

// loadSigned loads a generated signed integer field with truncation checking.
func loadSigned[T signed](ptr unsafe.Pointer, encoded wire.Object) bool {
	value := (*T)(ptr)
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
func saveUnsigned[T unsigned](s Sink, slot int, ptr unsafe.Pointer) {
	value := (*T)(ptr)
	if *value == 0 {
		*s.field(slot) = wire.Nil{}
		return
	}
	*s.field(slot) = wire.Uint(*value)
}

// loadUnsigned loads a generated unsigned integer field with truncation checking.
func loadUnsigned[T unsigned](ptr unsafe.Pointer, encoded wire.Object) bool {
	value := (*T)(ptr)
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
func saveBool[T ~bool](s Sink, slot int, ptr unsafe.Pointer) {
	value := (*T)(ptr)
	if !*value {
		*s.field(slot) = wire.Nil{}
		return
	}
	*s.field(slot) = wire.Bool(*value)
}

// loadBool loads a generated boolean field.
func loadBool[T ~bool](ptr unsafe.Pointer, encoded wire.Object) bool {
	value := (*T)(ptr)
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
func saveString[T ~string](s Sink, slot int, ptr unsafe.Pointer) {
	value := (*T)(ptr)
	if *value == "" {
		*s.field(slot) = wire.Nil{}
		return
	}
	x := wire.String(*value)
	*s.field(slot) = &x
}

// loadString loads a generated string field.
func loadString[T ~string](ptr unsafe.Pointer, encoded wire.Object) bool {
	value := (*T)(ptr)
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
func saveFloat[T floating](s Sink, slot int, ptr unsafe.Pointer) {
	value := (*T)(ptr)
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
func loadFloat[T floating](ptr unsafe.Pointer, encoded wire.Object) bool {
	value := (*T)(ptr)
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
func saveComplex[T complexNumber](s Sink, slot int, ptr unsafe.Pointer) {
	value := (*T)(ptr)
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
func loadComplex[T complexNumber](ptr unsafe.Pointer, encoded wire.Object) bool {
	value := (*T)(ptr)
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
