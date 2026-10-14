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

// Value contains one completely parsed wire value. LoadValue returns owned
// storage; ValueBuffer.Load returns a temporary view that Own can detach.
// The zero Value is invalid and is distinct from an encoded Nil.
//
// Scalar bits and pathless reference IDs are inline. payload points to either
// a static header or the start of an ordinary, correctly typed Go allocation.
// A temporary value instead points to a valueFrame and stores a node index.
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

func frameValue(frame *valueFrame, index int) Value {
	return Value{bits: uint64(index), payload: unsafe.Pointer(frame)}
}

func (v Value) frame() *valueFrame {
	if (*valueHeader)(v.payload).kind == frameKind {
		return (*valueFrame)(v.payload)
	}
	return nil
}

func (v Value) scalarBits() uint64 {
	if frame := v.frame(); frame != nil {
		return frame.node(int(v.bits)).a
	}
	return v.bits
}

// Valid reports whether v contains a parsed value.
func (v Value) Valid() bool {
	return v.payload != nil
}

// Kind returns the encoded kind. v must be valid.
func (v Value) Kind() Kind {
	if frame := v.frame(); frame != nil {
		return frame.node(int(v.bits)).kind
	}
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
	return v.scalarBits() != 0
}

// Int returns an Int value.
func (v Value) Int() int64 {
	v.require(KindInt)
	return int64(v.scalarBits())
}

// Uint returns a Uint value.
func (v Value) Uint() uint64 {
	v.require(KindUint)
	return v.scalarBits()
}

// Float returns a Float32 or Float64 value, preserving Float32 promotion.
func (v Value) Float() float64 {
	switch v.Kind() {
	case KindFloat32:
		return float64(math.Float32frombits(uint32(v.scalarBits())))
	case KindFloat64:
		return math.Float64frombits(v.scalarBits())
	default:
		panic("wire value is not a float")
	}
}

// Complex returns a Complex64 or Complex128 value.
func (v Value) Complex() complex128 {
	switch v.Kind() {
	case KindComplex64:
		bits := v.scalarBits()
		re := math.Float32frombits(uint32(bits))
		im := math.Float32frombits(uint32(bits >> 32))
		return complex128(complex(re, im))
	case KindComplex128:
		if frame := v.frame(); frame != nil {
			node := frame.node(int(v.bits))
			return complex(math.Float64frombits(node.a), math.Float64frombits(node.b))
		}
		return complex(math.Float64frombits(v.bits), math.Float64frombits((*complexValue)(v.payload).imaginary))
	default:
		panic("wire value is not complex")
	}
}

// Text returns an independently owned String value.
func (v Value) Text() string {
	v.require(KindString)
	if frame := v.frame(); frame != nil {
		return frame.strings[frame.node(int(v.bits)).a]
	}
	return (*stringValue)(v.payload).value
}

// Reference returns a Ref value. Dots and Type are read-only views.
func (v Value) Reference() Ref {
	if v.frame() != nil {
		return v.ReferenceView().materialize()
	}
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
	if v.frame() != nil {
		return v.SliceView().materialize()
	}
	v.require(KindSlice)
	p := (*sliceValue)(v.payload)
	return Slice{Length: Uint(v.bits), Capacity: p.capacity, Ref: p.ref.Reference()}
}

// Len returns the length of an Array, Map, or MultipleObjects value.
func (v Value) Len() int {
	if frame := v.frame(); frame != nil {
		node := frame.node(int(v.bits))
		switch node.kind {
		case KindArray, KindMap, KindMultipleObjects:
			return int(node.b)
		default:
			panic("wire value is not a collection")
		}
	}
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
	if frame := v.frame(); frame != nil {
		node := frame.node(int(v.bits))
		switch node.kind {
		case KindArray, KindMultipleObjects:
			if i < 0 || uint64(i) >= node.b {
				panic("wire element index out of range")
			}
			return frameValue(frame, int(node.a)+i)
		default:
			panic("wire value is not an array")
		}
	}
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
	if frame := v.frame(); frame != nil {
		node := frame.node(int(v.bits))
		if i < 0 || uint64(i) >= node.b {
			panic("wire entry index out of range")
		}
		index := int(node.a) + 2*i
		return frameValue(frame, index), frameValue(frame, index+1)
	}
	entry := (*mapValue)(v.payload).entries[i]
	return entry.key, entry.value
}

// StructType returns a Struct's wire type ID.
func (v Value) StructType() TypeID {
	v.require(KindStruct)
	return TypeID(v.scalarBits())
}

func (v Value) fieldBody() Value {
	if frame := v.frame(); frame != nil {
		return frameValue(frame, int(frame.node(int(v.bits)).b))
	}
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
	if v.frame() != nil {
		owned := v.Own()
		return owned.Interface()
	}
	p := (*interfaceValue)(v.payload)
	return p.typ, p.value
}

// Type returns read-only schema metadata for a Type value.
func (v Value) Type() *Type {
	v.require(KindType)
	if v.frame() != nil {
		return v.Own().Type()
	}
	return &(*typeValue)(v.payload).typ
}

// LoadValue returns a completely parsed, independently owned value. The parser
// is shared with ValueBuffer; Own is only a parsed-data ownership adapter.
//
// +checkescape:hard
//
// N.B. This function will panic on error.
func LoadValue(r *Reader) Value {
	buffer := new(ValueBuffer) // Escapes: temporary parsing storage.
	return buffer.Load(r).Own()
}

func (v Value) ownedSliceView() SliceView {
	p := (*sliceValue)(v.payload)
	return SliceView{Length: Uint(v.bits), Capacity: p.capacity, Ref: p.ref.ReferenceView()}
}

// Own detaches temporary data into the existing owned payload tree. Already
// owned values are returned unchanged: an owned Source's descendants share
// their existing children rather than copying overlapping subtrees. Nothing
// in the returned tree points back into a temporary frame.
func (v Value) Own() Value {
	if !v.Valid() || v.frame() == nil {
		return v
	}
	frame := v.frame()
	node := frame.node(int(v.bits))
	switch node.kind {
	case KindBool, KindInt, KindUint, KindFloat32, KindFloat64,
		KindComplex64, KindNil, KindNoObjects:
		return scalarValue(node.kind, node.a)
	case KindComplex128:
		p := &complexValue{valueHeader: valueHeader{node.kind}, imaginary: node.b} // Escapes: owned complex payload.
		return Value{bits: node.a, payload: unsafe.Pointer(p)}
	case KindString:
		value := v.Text()
		p := &stringValue{valueHeader: valueHeader{node.kind}, value: value} // Escapes: owned string payload.
		return Value{payload: unsafe.Pointer(p)}
	case KindRef:
		ref := v.ReferenceView().materialize()
		if len(ref.Dots) == 0 {
			return scalarValue(KindRef, uint64(ref.Root))
		}
		p := &refValue{valueHeader: valueHeader{KindRef}, dots: ref.Dots, typ: ref.Type} // Escapes: owned reference metadata.
		return Value{bits: uint64(ref.Root), payload: unsafe.Pointer(p)}
	case KindSlice:
		slice := v.SliceView()
		ref := slice.Ref.value.Own()
		p := &sliceValue{valueHeader: valueHeader{KindSlice}, capacity: slice.Capacity, ref: ref} // Escapes: owned slice payload.
		return Value{bits: uint64(slice.Length), payload: unsafe.Pointer(p)}
	case KindArray, KindMultipleObjects:
		p := &arrayValue{valueHeader: valueHeader{node.kind}} // Escapes: owned collection payload.
		if count := v.Len(); count != 0 || node.kind == KindMultipleObjects {
			p.values = make([]Value, count) // Escapes: owned collection elements.
			for i := range p.values {
				p.values[i] = v.Element(i).Own()
			}
		}
		return Value{payload: unsafe.Pointer(p)}
	case KindMap:
		p := &mapValue{valueHeader: valueHeader{KindMap}} // Escapes: owned map payload.
		if count := v.Len(); count != 0 {
			p.entries = make([]valuePair, count) // Escapes: owned map entries.
			for i := range p.entries {
				key, value := v.Entry(i)
				p.entries[i] = valuePair{key: key.Own(), value: value.Own()}
			}
		}
		return Value{payload: unsafe.Pointer(p)}
	case KindStruct:
		fields := v.fieldBody().Own()
		p := &structValue{valueHeader: valueHeader{KindStruct}, fields: fields} // Escapes: owned struct payload.
		return Value{bits: node.a, payload: unsafe.Pointer(p)}
	case KindInterface:
		typ, inner := v.InterfaceView()
		ownedType, value := typ.materialize(), inner.Own()
		p := &interfaceValue{valueHeader: valueHeader{KindInterface}, typ: ownedType, value: value} // Escapes: owned interface payload.
		return Value{payload: unsafe.Pointer(p)}
	case KindType:
		p := &typeValue{valueHeader: valueHeader{KindType}} // Escapes: owned schema payload.
		p.typ.Name = frame.strings[node.a]
		fields := frame.node(int(node.b))
		p.typ.Fields = make([]string, int(fields.b)) // Escapes: owned schema field headers.
		for i := range p.typ.Fields {
			p.typ.Fields[i] = frame.strings[frame.node(int(fields.a)+i).a]
		}
		return Value{payload: unsafe.Pointer(p)}
	default:
		panic("invalid parsed value")
	}
}

// materialize transfers a freshly parsed value to the mutable Object API.
// It reads no wire bytes and is not used by state restoration.
func materialize(v Value) Object {
	switch v.Kind() {
	case KindBool:
		return Bool(v.Bool())
	case KindInt:
		value := Int(v.Int())
		return value // Escapes: boxing the decoded scalar.
	case KindUint:
		value := Uint(v.Uint())
		return value // Escapes: boxing the decoded scalar.
	case KindFloat32:
		value := Float32(math.Float32frombits(uint32(v.scalarBits())))
		return value // Escapes: boxing the decoded scalar.
	case KindFloat64:
		value := Float64(v.Float())
		return value // Escapes: boxing the decoded scalar.
	case KindComplex64:
		var c Complex64 // Escapes: mutable Object storage.
		c = Complex64(v.Complex())
		return &c
	case KindComplex128:
		var c Complex128 // Escapes: mutable Object storage.
		c = Complex128(v.Complex())
		return &c
	case KindNil:
		return Nil{}
	case KindNoObjects:
		return noObjects{}
	case KindString:
		var s String // Escapes: mutable Object storage.
		s = String(v.Text())
		return &s
	case KindRef:
		var r Ref // Escapes: mutable Object storage.
		r = v.Reference()
		if len(r.Dots) == 0 {
			r.Dots = make([]Dot, 0)
		}
		return &r
	case KindSlice:
		var s Slice // Escapes: mutable Object storage.
		s = v.Slice()
		if len(s.Ref.Dots) == 0 {
			s.Ref.Dots = make([]Dot, 0)
		}
		return &s
	case KindArray:
		a := &Array{} // Escapes: mutable Object storage.
		if v.Len() != 0 {
			length := v.Len()
			a.Contents = make([]Object, length) // Escapes: mutable array elements.
			for i := range a.Contents {
				a.Contents[i] = materialize(v.Element(i))
			}
		}
		return a
	case KindMap:
		m := &Map{} // Escapes: mutable Object storage.
		if v.Len() != 0 {
			length := v.Len()
			m.Keys, m.Values = make([]Object, length), make([]Object, length) // Escapes: mutable map entries.
			for i := range m.Keys {
				key, value := v.Entry(i)
				m.Keys[i], m.Values[i] = materialize(key), materialize(value)
			}
		}
		return m
	case KindStruct:
		id, fields := v.StructType(), materialize(v.fieldBody())
		return &Struct{TypeID: id, fields: fields} // Escapes: mutable Object storage.
	case KindMultipleObjects:
		length := v.Len()
		m := make(multipleObjects, length) // Escapes: mutable Object storage and fields.
		for i := range m {
			m[i] = materialize(v.Element(i))
		}
		return &m
	case KindInterface:
		typ, value := v.InterfaceView()
		ownedType := typ.materialize()
		obj := materialize(value)
		return &Interface{Type: ownedType, Value: obj} // Escapes: mutable Object storage.
	case KindType:
		return v.Type()
	default:
		panic("unreachable: parsed value has unknown kind")
	}
}
