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

// Value owns one completely parsed wire value. Copies remain valid after the
// reader advances. Accessors return read-only views of its owned storage.
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
		panic(fmt.Sprintf("wire value has kind %d, wanted %d", v.Kind(), kind))
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
		re := math.Float32frombits(uint32(v.bits))
		im := math.Float32frombits(uint32(v.bits >> 32))
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

// Fields returns a Struct's encoded field count. This is separate from schema
// reconciliation: the legacy singleton body ignores its requested field index.
func (v Value) Fields() int {
	v.require(KindStruct)
	body := (*structValue)(v.payload).fields
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
	body := (*structValue)(v.payload).fields
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

// Type returns read-only schema metadata for a Type value.
func (v Value) Type() *Type {
	v.require(KindType)
	return &(*typeValue)(v.payload).typ
}

// LoadValue parses one complete value before returning it. This is the sole
// data-value grammar used by state restoration and the Object adapter.
// N.B. This function will panic on error.
func LoadValue(r *Reader) Value {
	return loadValuePayload(r, Kind(loadUint(r)))
}

func loadReferenceValue(r *Reader) Value {
	ref := loadRef(r)
	if len(ref.Dots) == 0 {
		return scalarValue(KindRef, uint64(ref.Root))
	}
	p := &refValue{valueHeader: valueHeader{KindRef}, dots: ref.Dots, typ: ref.Type}
	return Value{bits: uint64(ref.Root), payload: unsafe.Pointer(p)}
}

func loadValuePayload(r *Reader, kind Kind) Value {
	switch kind {
	case KindBool:
		var bits uint64
		if loadBool(r) {
			bits = 1
		}
		return scalarValue(kind, bits)
	case KindInt:
		return scalarValue(kind, uint64(loadInt(r)))
	case KindUint:
		return scalarValue(kind, uint64(loadUint(r)))
	case KindFloat32:
		return scalarValue(kind, uint64(uint32(loadUint(r))))
	case KindFloat64:
		return scalarValue(kind, uint64(loadUint(r)))
	case KindComplex64:
		re, im := uint32(loadUint(r)), uint32(loadUint(r))
		return scalarValue(kind, uint64(re)|uint64(im)<<32)
	case KindComplex128:
		re, im := uint64(loadUint(r)), uint64(loadUint(r))
		p := &complexValue{valueHeader: valueHeader{kind}, imaginary: im}
		return Value{bits: re, payload: unsafe.Pointer(p)}
	case KindNil, KindNoObjects:
		return scalarValue(kind, 0)
	case KindRef:
		return loadReferenceValue(r)
	case KindString:
		p := &stringValue{valueHeader: valueHeader{kind}, value: string(loadString(r))}
		return Value{payload: unsafe.Pointer(p)}
	case KindSlice:
		length, capacity := loadUint(r), loadUint(r)
		p := &sliceValue{valueHeader: valueHeader{kind}, capacity: capacity, ref: loadReferenceValue(r)}
		return Value{bits: uint64(length), payload: unsafe.Pointer(p)}
	case KindArray:
		length := loadUint(r)
		p := &arrayValue{valueHeader: valueHeader{kind}}
		if length != 0 {
			p.values = make([]Value, length)
			p.values[0] = LoadValue(r)
			elementKind := p.values[0].Kind()
			for i := 1; i < len(p.values); i++ {
				p.values[i] = loadValuePayload(r, elementKind)
			}
		}
		return Value{payload: unsafe.Pointer(p)}
	case KindMap:
		length := loadUint(r)
		p := &mapValue{valueHeader: valueHeader{kind}}
		if length != 0 {
			p.entries = make([]valuePair, length)
			p.entries[0] = valuePair{key: LoadValue(r), value: LoadValue(r)}
			keyKind, valueKind := p.entries[0].key.Kind(), p.entries[0].value.Kind()
			for i := 1; i < len(p.entries); i++ {
				p.entries[i] = valuePair{key: loadValuePayload(r, keyKind), value: loadValuePayload(r, valueKind)}
			}
		}
		return Value{payload: unsafe.Pointer(p)}
	case KindStruct:
		id := loadUint(r)
		p := &structValue{valueHeader: valueHeader{kind}, fields: LoadValue(r)}
		return Value{bits: uint64(id), payload: unsafe.Pointer(p)}
	case KindMultipleObjects:
		p := &arrayValue{valueHeader: valueHeader{kind}, values: make([]Value, loadUint(r))}
		for i := range p.values {
			p.values[i] = LoadValue(r)
		}
		return Value{payload: unsafe.Pointer(p)}
	case KindInterface:
		p := &interfaceValue{valueHeader: valueHeader{kind}, typ: loadTypeSpec(r), value: LoadValue(r)}
		return Value{payload: unsafe.Pointer(p)}
	case KindType:
		p := &typeValue{valueHeader: valueHeader{kind}}
		p.typ.Name = string(loadString(r))
		p.typ.Fields = make([]string, loadUint(r))
		for i := range p.typ.Fields {
			p.typ.Fields[i] = string(loadString(r))
		}
		return Value{payload: unsafe.Pointer(p)}
	default:
		// Kind retains the full tag width; no unknown high bits are discarded.
		panic(fmt.Errorf("unknown header: %d", kind))
	}
}

// materialize transfers a freshly parsed value to the mutable Object API.
// It reads no wire bytes and is not used by state restoration.
func materialize(v Value) Object {
	switch v.Kind() {
	case KindBool:
		return Bool(v.Bool())
	case KindInt:
		return Int(v.Int())
	case KindUint:
		return Uint(v.Uint())
	case KindFloat32:
		return Float32(math.Float32frombits(uint32(v.bits)))
	case KindFloat64:
		return Float64(v.Float())
	case KindComplex64:
		c := Complex64(v.Complex())
		return &c
	case KindComplex128:
		c := Complex128(v.Complex())
		return &c
	case KindNil:
		return Nil{}
	case KindNoObjects:
		return noObjects{}
	case KindString:
		s := String(v.Text())
		return &s
	case KindRef:
		r := v.Reference()
		if len(r.Dots) == 0 {
			r.Dots = make([]Dot, 0)
		}
		return &r
	case KindSlice:
		s := v.Slice()
		if len(s.Ref.Dots) == 0 {
			s.Ref.Dots = make([]Dot, 0)
		}
		return &s
	case KindArray:
		a := &Array{}
		if v.Len() != 0 {
			a.Contents = make([]Object, v.Len())
			for i := range a.Contents {
				a.Contents[i] = materialize(v.Element(i))
			}
		}
		return a
	case KindMap:
		m := &Map{}
		if v.Len() != 0 {
			m.Keys, m.Values = make([]Object, v.Len()), make([]Object, v.Len())
			for i := range m.Keys {
				key, value := v.Entry(i)
				m.Keys[i], m.Values[i] = materialize(key), materialize(value)
			}
		}
		return m
	case KindStruct:
		return &Struct{TypeID: v.StructType(), fields: materialize((*structValue)(v.payload).fields)}
	case KindMultipleObjects:
		m := make(multipleObjects, v.Len())
		for i := range m {
			m[i] = materialize(v.Element(i))
		}
		return &m
	case KindInterface:
		typ, value := v.Interface()
		return &Interface{Type: typ, Value: materialize(value)}
	case KindType:
		return v.Type()
	default:
		panic("unreachable: parsed value has unknown kind")
	}
}
