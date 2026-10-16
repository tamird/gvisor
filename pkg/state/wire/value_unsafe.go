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

package wire

import (
	"fmt"
	"math"
	"unsafe"
)

// Kind identifies the encoded form of a Value. It does not identify the Go
// destination type; for example, an encoded Array can fill an existing slice.
type Kind Uint

// Kinds use the existing wire tags without narrowing their integer width.
const (
	KindBool            = Kind(typeBool)
	KindInt             = Kind(typeInt)
	KindUint            = Kind(typeUint)
	KindFloat32         = Kind(typeFloat32)
	KindFloat64         = Kind(typeFloat64)
	KindNil             = Kind(typeNil)
	KindRef             = Kind(typeRef)
	KindString          = Kind(typeString)
	KindSlice           = Kind(typeSlice)
	KindArray           = Kind(typeArray)
	KindMap             = Kind(typeMap)
	KindStruct          = Kind(typeStruct)
	KindNoObjects       = Kind(typeNoObjects)
	KindMultipleObjects = Kind(typeMultipleObjects)
	KindInterface       = Kind(typeInterface)
	KindComplex64       = Kind(typeComplex64)
	KindComplex128      = Kind(typeComplex128)
	KindType            = Kind(typeType)
)

// Value contains one completely parsed, independently owned wire value.
// Children and immutable strings remain valid independently of the reader.
// The zero Value is invalid and is distinct from an encoded Nil.
//
// Scalar bits and pathless reference IDs are inline. payload points to either
// a static header or the start of an ordinary, correctly typed Go allocation.
// Every payload allocation begins with valueHeader. Checked accessors recover
// its original type; pointers are never stored in bits.
type Value struct {
	bits    uint64
	payload unsafe.Pointer
}

type valueHeader struct {
	kind Kind
}

var valueHeaders = [...]valueHeader{
	{KindBool}, {KindInt}, {KindUint}, {KindFloat32}, {KindFloat64},
	{KindNil}, {KindRef}, {KindString}, {KindSlice}, {KindArray},
	{KindMap}, {KindStruct}, {KindNoObjects}, {KindMultipleObjects},
	{KindInterface}, {KindComplex64}, {KindComplex128}, {KindType},
}

type stringValue struct {
	valueHeader
	value string
}

type refValue struct {
	valueHeader
	dots []Dot
	typ  TypeSpec
}

type sliceValue struct {
	valueHeader
	capacity Uint
	ref      Value
}

type arrayValue struct {
	valueHeader
	values []Value
}

type valuePair struct {
	key, value Value
}

type mapValue struct {
	valueHeader
	entries []valuePair
}

type structValue struct {
	valueHeader
	fields Value
}

type interfaceValue struct {
	valueHeader
	typ   TypeSpec
	value Value
}

type complexValue struct {
	valueHeader
	imaginary uint64
}

type typeValue struct {
	valueHeader
	typ Type
}

func scalarValue(kind Kind, bits uint64) Value {
	return Value{bits: bits, payload: unsafe.Pointer(&valueHeaders[kind])}
}

// Valid reports whether v contains a parsed value.
func (v Value) Valid() bool {
	return v.payload != nil
}

// Kind returns the encoded kind. v must be valid.
func (v Value) Kind() Kind {
	return (*valueHeader)(v.payload).kind
}

func (v Value) require(kind Kind) {
	if v.Kind() != kind {
		panic(fmt.Sprintf("wire value has kind %d, wanted %d", v.Kind(), kind)) // Escapes: error formatting.
	}
}

// Bool returns a Bool value.
func (v Value) Bool() bool {
	v.require(KindBool)
	return v.bits != 0
}

// Int returns an Int value.
func (v Value) Int() int64 {
	v.require(KindInt)
	return int64(v.bits)
}

// Uint returns a Uint value.
func (v Value) Uint() uint64 {
	v.require(KindUint)
	return v.bits
}

// Float returns a Float32 or Float64 value, preserving Float32 promotion.
func (v Value) Float() float64 {
	switch v.Kind() {
	case KindFloat32:
		return float64(math.Float32frombits(uint32(v.bits)))
	case KindFloat64:
		return math.Float64frombits(v.bits)
	default:
		panic("wire value is not a float")
	}
}

// Complex returns a Complex64 or Complex128 value.
func (v Value) Complex() complex128 {
	switch v.Kind() {
	case KindComplex64:
		bits := v.bits
		re := math.Float32frombits(uint32(bits))
		im := math.Float32frombits(uint32(bits >> 32))
		return complex128(complex(re, im))
	case KindComplex128:
		return complex(math.Float64frombits(v.bits), math.Float64frombits((*complexValue)(v.payload).imaginary))
	default:
		panic("wire value is not complex")
	}
}

// Text returns an independently owned String value.
func (v Value) Text() string {
	v.require(KindString)
	return (*stringValue)(v.payload).value
}

// Reference returns a Ref value. Dots and Type are read-only views.
func (v Value) Reference() Ref {
	v.require(KindRef)
	r := Ref{Root: Uint(v.bits)}
	if v.payload != unsafe.Pointer(&valueHeaders[KindRef]) {
		p := (*refValue)(v.payload)
		r.Dots, r.Type = p.dots, p.typ
	}
	return r
}

// Slice returns a Slice value. Its reference metadata is read-only.
func (v Value) Slice() Slice {
	v.require(KindSlice)
	p := (*sliceValue)(v.payload)
	return Slice{Length: Uint(v.bits), Capacity: p.capacity, Ref: p.ref.Reference()}
}

// Len returns the length of an Array, Map, or MultipleObjects value.
func (v Value) Len() int {
	switch v.Kind() {
	case KindArray, KindMultipleObjects:
		return len((*arrayValue)(v.payload).values)
	case KindMap:
		return len((*mapValue)(v.payload).entries)
	default:
		panic("wire value is not a collection")
	}
}

// Element returns an Array or MultipleObjects element.
func (v Value) Element(i int) Value {
	switch v.Kind() {
	case KindArray, KindMultipleObjects:
		return (*arrayValue)(v.payload).values[i]
	default:
		panic("wire value is not an array")
	}
}

// Entry returns a Map's key and value in their encoded order.
func (v Value) Entry(i int) (Value, Value) {
	v.require(KindMap)
	entry := (*mapValue)(v.payload).entries[i]
	return entry.key, entry.value
}

// StructType returns a Struct's wire type ID.
func (v Value) StructType() TypeID {
	v.require(KindStruct)
	return TypeID(v.bits)
}

func (v Value) fieldBody() Value {
	return (*structValue)(v.payload).fields
}

// Fields returns a Struct's encoded field count. This is separate from schema
// reconciliation: the legacy singleton body ignores its requested field index.
func (v Value) Fields() int {
	v.require(KindStruct)
	body := v.fieldBody()
	switch body.Kind() {
	case KindNoObjects:
		return 0
	case KindMultipleObjects:
		return body.Len()
	default:
		return 1
	}
}

// Field selects a Struct field, retaining the existing body representation's
// index behavior. Returned Values may be read repeatedly and in any order.
func (v Value) Field(i int) Value {
	v.require(KindStruct)
	body := v.fieldBody()
	switch body.Kind() {
	case KindNoObjects:
		panic("Field called inappropriately, wrong Alloc?")
	case KindMultipleObjects:
		return body.Element(i)
	default:
		return body
	}
}

// Interface returns an Interface's read-only type specification and value.
func (v Value) Interface() (TypeSpec, Value) {
	v.require(KindInterface)
	p := (*interfaceValue)(v.payload)
	return p.typ, p.value
}

// Type returns read-only schema metadata for a Type value. The schema owns its
// storage independently of the reader that parsed it.
func (v Value) Type() *Type {
	v.require(KindType)
	return &(*typeValue)(v.payload).typ
}
