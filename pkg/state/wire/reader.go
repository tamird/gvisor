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
	"io"
	"math"

	"gvisor.dev/gvisor/pkg/gohacks"
)

// Kind identifies a value's existing wire encoding.
type Kind Uint

// These names expose the existing tags without changing their encoding.
const (
	BoolKind            = Kind(typeBool)
	IntKind             = Kind(typeInt)
	UintKind            = Kind(typeUint)
	Float32Kind         = Kind(typeFloat32)
	Float64Kind         = Kind(typeFloat64)
	NilKind             = Kind(typeNil)
	RefKind             = Kind(typeRef)
	StringKind          = Kind(typeString)
	SliceKind           = Kind(typeSlice)
	ArrayKind           = Kind(typeArray)
	MapKind             = Kind(typeMap)
	StructKind          = Kind(typeStruct)
	NoObjectsKind       = Kind(typeNoObjects)
	MultipleObjectsKind = Kind(typeMultipleObjects)
	InterfaceKind       = Kind(typeInterface)
	Complex64Kind       = Kind(typeComplex64)
	Complex128Kind      = Kind(typeComplex128)
	TypeKind            = Kind(typeType)
)

// parser supplies the one grammar with either stream or bounded-memory input.
// A capturing stream owns data; a memory parser only advances offset. Neither
// mode buffers bytes beyond the value currently being parsed.
type parser struct {
	stream  *Reader
	data    []byte
	offset  int
	capture bool
}

func (p *parser) byte() byte {
	if p.stream != nil {
		b := p.stream.readByte()
		if p.capture {
			p.data = append(p.data, b)
		}
		return b
	}
	if p.offset == len(p.data) {
		panic(io.ErrUnexpectedEOF)
	}
	b := p.data[p.offset]
	p.offset++
	return b
}

func checkedCount(n Uint) int {
	if n > Uint(^uint(0)>>1) {
		panic(fmt.Errorf("wire length %d overflows int", n))
	}
	return int(n)
}

func (p *parser) bytes(n int) []byte {
	if p.stream == nil {
		if n > len(p.data)-p.offset {
			panic(io.ErrUnexpectedEOF)
		}
		b := p.data[p.offset : p.offset+n]
		p.offset += n
		return b
	}
	if p.capture {
		start := len(p.data)
		if n > int(^uint(0)>>1)-start {
			panic("wire value size overflows int")
		}
		p.data = append(p.data, make([]byte, n)...)
		b := p.data[start:]
		readFull(p.stream, b)
		return b
	}
	b := make([]byte, n)
	readFull(p.stream, b)
	return b
}

func (p *parser) uint() Uint {
	var u Uint
	for i := uint(0); i <= 9; i++ {
		b := p.byte()
		if b < 0x80 {
			if i == 9 && b > 1 {
				panic("overflow")
			}
			return u | Uint(b)<<(7*i)
		}
		u |= Uint(b&0x7f) << (7 * i)
	}
	panic("unreachable")
}

func (p *parser) int() Int {
	u := p.uint()
	x := Int(u >> 1)
	if u&1 != 0 {
		x = ^x
	}
	return x
}

func (p *parser) float32() Float32 {
	return Float32(math.Float32frombits(uint32(p.uint())))
}

func (p *parser) float64() Float64 {
	return Float64(math.Float64frombits(uint64(p.uint())))
}

func (p *parser) complex64() Complex64 {
	re, im := p.float32(), p.float32()
	return Complex64(complex(float32(re), float32(im)))
}

func (p *parser) complex128() Complex128 {
	re, im := p.float64(), p.float64()
	return Complex128(complex(float64(re), float64(im)))
}

func (p *parser) text(build bool) string {
	b := p.bytes(checkedCount(p.uint()))
	if !build {
		return ""
	}
	if p.stream != nil && !p.capture {
		// bytes allocated precisely this string's immutable storage.
		return gohacks.StringFromImmutableBytes(b)
	}
	// A field string must not retain its enclosing captured map/record.
	return string(b)
}

func (p *parser) reference(dst *Ref) {
	root := p.uint()
	count := checkedCount(p.uint())
	if dst != nil {
		dst.Root = root
		dst.Dots = make([]Dot, count)
	}
	for i := 0; i < count; i++ {
		d := p.int()
		if d >= 0 {
			if dst != nil {
				dst.Dots[i] = Index(d)
			}
		} else {
			b := p.bytes(checkedCount(Uint(-d)))
			if dst != nil {
				name := FieldName(string(b))
				dst.Dots[i] = &name
			}
		}
	}
	if count != 0 {
		typ := p.typeSpec(dst != nil)
		if dst != nil {
			dst.Type = typ
		}
	}
}

func (p *parser) typeSpec(build bool) TypeSpec {
	switch kind := p.uint(); kind {
	case typeSpecTypeID:
		id := p.uint()
		if build {
			return TypeID(id)
		}
	case typeSpecPointer:
		typ := p.typeSpec(build)
		if build {
			return &TypeSpecPointer{Type: typ}
		}
	case typeSpecArray:
		count, typ := p.uint(), p.typeSpec(build)
		if build {
			return &TypeSpecArray{Count: count, Type: typ}
		}
	case typeSpecSlice:
		typ := p.typeSpec(build)
		if build {
			return &TypeSpecSlice{Type: typ}
		}
	case typeSpecMap:
		key, value := p.typeSpec(build), p.typeSpec(build)
		if build {
			return &TypeSpecMap{Key: key, Value: value}
		}
	case typeSpecNil:
		if build {
			return TypeSpecNil{}
		}
	default:
		panic(fmt.Errorf("unknown type header: %d", kind))
	}
	return nil
}

// object validates exactly one body. build is reserved for the maintained
// wire.Load/pretty adapter; state data values never request an Object tree.
func (p *parser) object(kind Kind, build bool) Object {
	switch kind {
	case BoolKind:
		v := p.uint() == 1
		if build {
			return Bool(v)
		}
	case IntKind:
		v := p.int()
		if build {
			return v
		}
	case UintKind:
		v := p.uint()
		if build {
			return v
		}
	case Float32Kind:
		v := p.float32()
		if build {
			return v
		}
	case Float64Kind:
		v := p.float64()
		if build {
			return v
		}
	case Complex64Kind:
		v := p.complex64()
		if build {
			owned := v
			return &owned
		}
	case Complex128Kind:
		v := p.complex128()
		if build {
			owned := v
			return &owned
		}
	case NilKind:
		if build {
			return Nil{}
		}
	case StringKind:
		v := String(p.text(build))
		if build {
			owned := v
			return &owned
		}
	case RefKind:
		if build {
			v := new(Ref)
			p.reference(v)
			return v
		}
		p.reference(nil)
	case SliceKind:
		length, capacity := p.uint(), p.uint()
		if build {
			v := &Slice{Length: length, Capacity: capacity}
			p.reference(&v.Ref)
			return v
		}
		p.reference(nil)
	case ArrayKind:
		count := checkedCount(p.uint())
		var contents []Object
		if build && count != 0 {
			contents = make([]Object, count)
		}
		var element Kind
		for i := 0; i < count; i++ {
			if i == 0 {
				element = Kind(p.uint())
			}
			v := p.object(element, build)
			if build {
				contents[i] = v
			}
		}
		if build {
			return &Array{Contents: contents}
		}
	case MapKind:
		count := checkedCount(p.uint())
		var keys, values []Object
		if build && count != 0 {
			keys, values = make([]Object, count), make([]Object, count)
		}
		var keyKind, valueKind Kind
		for i := 0; i < count; i++ {
			if i == 0 {
				keyKind = Kind(p.uint())
			}
			key := p.object(keyKind, build)
			if i == 0 {
				valueKind = Kind(p.uint())
			}
			value := p.object(valueKind, build)
			if build {
				keys[i], values[i] = key, value
			}
		}
		if build {
			return &Map{Keys: keys, Values: values}
		}
	case StructKind:
		id := TypeID(p.uint())
		fields := p.object(Kind(p.uint()), build)
		if build {
			return &Struct{TypeID: id, fields: fields}
		}
	case NoObjectsKind:
		if build {
			return noObjects{}
		}
	case MultipleObjectsKind:
		count := checkedCount(p.uint())
		var values multipleObjects
		if build {
			values = make(multipleObjects, count)
		}
		for i := 0; i < count; i++ {
			v := p.object(Kind(p.uint()), build)
			if build {
				values[i] = v
			}
		}
		if build {
			owned := values
			return &owned
		}
	case InterfaceKind:
		typ := p.typeSpec(build)
		value := p.object(Kind(p.uint()), build)
		if build {
			return &Interface{Type: typ, Value: value}
		}
	case TypeKind:
		name := p.text(build)
		count := checkedCount(p.uint())
		var fields []string
		if build {
			fields = make([]string, count)
		}
		for i := 0; i < count; i++ {
			field := p.text(build)
			if build {
				fields[i] = field
			}
		}
		if build {
			return &Type{Name: name, Fields: fields}
		}
	default:
		panic(fmt.Errorf("unknown header: %d", kind))
	}
	return nil
}

// Value is an immutable, fully validated encoded value. Its private body may
// share an enclosing value's storage, including when retained by a nested
// Source. Primitive accessors return owned Go data rather than mutable aliases.
type Value struct {
	kind Kind
	body []byte
}

// Kind returns the encoding of v.
func (v Value) Kind() Kind { return v.kind }

func (v Value) parser(kind Kind) parser {
	if v.kind != kind {
		panic(fmt.Errorf("wire kind %d, wanted %d", v.kind, kind))
	}
	return parser{data: v.body}
}

// ReadKind consumes only the next wire tag.
func ReadKind(r *Reader) Kind {
	p := parser{stream: r}
	return Kind(p.uint())
}

// ReadValue consumes and validates one complete value before exposing its
// contents. It does not read ahead into any following value or non-state data.
func ReadValue(r *Reader) Value {
	kind := ReadKind(r)
	p := parser{stream: r, capture: true}
	p.object(kind, false)
	return Value{kind: kind, body: p.data}
}

// ReadType consumes a type declaration after its TypeKind tag has been read.
func ReadType(r *Reader) *Type {
	p := parser{stream: r}
	return p.object(TypeKind, true).(*Type)
}

// Load retains the Object API for pretty/debug readers using the same grammar.
func Load(r *Reader) Object {
	p := parser{stream: r}
	return p.object(Kind(p.uint()), true)
}

// Object materializes v for error reporting and other Object consumers.
func (v Value) Object() Object {
	p := parser{data: v.body}
	return p.object(v.kind, true)
}

// IsNil reports the universal wire no-op, including zero-root references and
// slices. Their metadata has already been fully validated by ReadValue.
func (v Value) IsNil() bool {
	p := parser{data: v.body}
	switch v.kind {
	case NilKind:
		return true
	case RefKind:
		return p.uint() == 0
	case SliceKind:
		p.uint()
		p.uint()
		return p.uint() == 0
	default:
		return false
	}
}

// Bool returns a BoolKind value.
func (v Value) Bool() Bool { p := v.parser(BoolKind); return Bool(p.uint() == 1) }

// Int returns an IntKind value.
func (v Value) Int() Int { p := v.parser(IntKind); return p.int() }

// Uint returns a UintKind value.
func (v Value) Uint() Uint { p := v.parser(UintKind); return p.uint() }

// Float32 returns a Float32Kind value.
func (v Value) Float32() Float32 { p := v.parser(Float32Kind); return p.float32() }

// Float64 returns a Float64Kind value.
func (v Value) Float64() Float64 { p := v.parser(Float64Kind); return p.float64() }

// Complex64 returns a Complex64Kind value.
func (v Value) Complex64() Complex64 { p := v.parser(Complex64Kind); return p.complex64() }

// Complex128 returns a Complex128Kind value.
func (v Value) Complex128() Complex128 { p := v.parser(Complex128Kind); return p.complex128() }

// Text returns independently owned text from a StringKind value.
func (v Value) Text() string { p := v.parser(StringKind); return p.text(true) }

// Ref returns a RefKind value without a heap wrapper around the reference.
func (v Value) Ref() Ref {
	p := v.parser(RefKind)
	var ref Ref
	p.reference(&ref)
	return ref
}

// Slice returns a SliceKind value.
func (v Value) Slice() Slice {
	p := v.parser(SliceKind)
	s := Slice{Length: p.uint(), Capacity: p.uint()}
	p.reference(&s.Ref)
	return s
}

// view validates the next bounded body and returns its immutable region.
func (p *parser) view(kind Kind) Value {
	start := p.offset
	p.object(kind, false)
	return Value{kind: kind, body: p.data[start:p.offset]}
}

// Fields owns repeatable random field access, including schema permutations.
// Singleton access intentionally ignores the index, as Struct.Field does.
type Fields struct {
	TypeID   TypeID
	single   Value
	multiple []Value
}

// Struct returns a struct's fields, indexing only the multiple-field form.
func (v Value) Struct() *Fields {
	p := v.parser(StructKind)
	f := &Fields{TypeID: TypeID(p.uint())}
	kind := Kind(p.uint())
	if kind != MultipleObjectsKind {
		f.single = Value{kind: kind, body: p.data[p.offset:]}
		return f
	}
	count := checkedCount(p.uint())
	f.single.kind = MultipleObjectsKind
	f.multiple = make([]Value, count)
	for i := range f.multiple {
		f.multiple[i] = p.view(Kind(p.uint()))
	}
	return f
}

// Len reports the number of encoded fields, independently of the schema.
func (f *Fields) Len() int {
	switch f.single.kind {
	case NoObjectsKind:
		return 0
	case MultipleObjectsKind:
		return len(f.multiple)
	default:
		return 1
	}
}

// Field returns a repeatable view of a wire slot.
func (f *Fields) Field(i int) Value {
	switch f.single.kind {
	case NoObjectsKind:
		panic("Field called inappropriately, wrong Alloc?")
	case MultipleObjectsKind:
		return f.multiple[i]
	default:
		return f.single
	}
}

// Sequence traverses a homogeneous array or map with no per-element index.
// Each iterator owns only a cursor; its returned views remain independently
// valid after the cursor advances.
type Sequence struct {
	input     parser
	remaining int
	first     bool
	keyKind   Kind
	valueKind Kind
}

// Array returns the length and a bounded iterator over an ArrayKind value.
func (v Value) Array() (int, Sequence) {
	p := v.parser(ArrayKind)
	count := checkedCount(p.uint())
	return count, Sequence{input: p, remaining: count, first: true}
}

// Map returns a bounded iterator over a MapKind value.
func (v Value) Map() Sequence {
	p := v.parser(MapKind)
	count := checkedCount(p.uint())
	return Sequence{input: p, remaining: count, first: true}
}

// Next returns the next array element.
func (s *Sequence) Next() (Value, bool) {
	if s.remaining == 0 {
		return Value{}, false
	}
	if s.first {
		s.valueKind = Kind(s.input.uint())
		s.first = false
	}
	s.remaining--
	return s.input.view(s.valueKind), true
}

// NextPair returns both views of the next map entry before either can be
// handed to a decoder or hook. There is no key-based lookup (including NaNs).
func (s *Sequence) NextPair() (Value, Value, bool) {
	if s.remaining == 0 {
		return Value{}, Value{}, false
	}
	if s.first {
		s.keyKind = Kind(s.input.uint())
	}
	key := s.input.view(s.keyKind)
	if s.first {
		s.valueKind = Kind(s.input.uint())
		s.first = false
	}
	value := s.input.view(s.valueKind)
	s.remaining--
	return key, value, true
}

// Interface returns the dynamic type and its bounded value.
func (v Value) Interface() (TypeSpec, Value) {
	p := v.parser(InterfaceKind)
	typ := p.typeSpec(true)
	kind := Kind(p.uint())
	return typ, Value{kind: kind, body: p.data[p.offset:]}
}
