// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package wire

import "math"

// Scalar holds a primitive while it crosses the generated-code boundary.
// Numeric values retain their full wire width until assignment is checked.
type Scalar struct {
	Kind uint64
	Int  int64
	Uint uint64
	Real float64
	Imag float64
	Text string
}

// ScalarBool and the related constants are existing wire tags. ScalarObject
// denotes a field whose captured value keeps the ordinary Object interface.
const (
	ScalarBool       = uint64(typeBool)
	ScalarInt        = uint64(typeInt)
	ScalarUint       = uint64(typeUint)
	ScalarFloat32    = uint64(typeFloat32)
	ScalarFloat64    = uint64(typeFloat64)
	ScalarNil        = uint64(typeNil)
	ScalarString     = uint64(typeString)
	ScalarComplex64  = uint64(typeComplex64)
	ScalarComplex128 = uint64(typeComplex128)
	ScalarObject     = uint64(math.MaxUint64)
)

type captureSlot struct {
	kind   uint64
	offset int
}

// CaptureLayout describes wire-order fields. The state type-info owner derives
// it once per stream; wire does not register or reconcile native Go types.
type CaptureLayout struct {
	slots                         []captureSlot
	words, objects, strings, mask int
	scalars                       int
}

// NewCaptureLayout assigns storage without per-object field spans or lengths.
func NewCaptureLayout(kinds []uint64) *CaptureLayout {
	l := &CaptureLayout{slots: make([]captureSlot, len(kinds)), mask: (len(kinds) + 63) / 64}
	l.words = 2 * l.mask // Written and non-nil bits preserve unfilled fields.
	for i, kind := range kinds {
		s := captureSlot{kind: kind}
		switch kind {
		case ScalarObject:
			s.offset = l.objects
			l.objects++
		case ScalarString:
			s.offset = l.strings
			l.strings++
			l.scalars++
		case ScalarBool, ScalarInt, ScalarUint, ScalarFloat32, ScalarFloat64:
			s.offset = l.words
			l.words++
			l.scalars++
		case ScalarComplex64, ScalarComplex128:
			s.offset = l.words
			l.words += 2
			l.scalars++
		default:
			panic("invalid captured field kind")
		}
		l.slots[i] = s
	}
	return l
}

// CaptureArena owns typed storage for one stream. Fixed blocks keep Object
// addresses stable while the graph resolver rewrites reference handles. Strings
// and Objects remain GC-visible; no uintptr is used to retain application data.
type CaptureArena struct {
	words                               []*[128]uint64
	objects                             []*[32]Object
	strings                             []*[32]string
	fields                              []*[16]captureFields
	nWords, nObjects, nStrings, nFields int
}

type captureFields struct {
	arena                   *CaptureArena
	layout                  *CaptureLayout
	words, objects, strings int
	overrides               map[int]*Object
}

func (a *CaptureArena) word(i int) *uint64   { return &a.words[i/128][i%128] }
func (a *CaptureArena) object(i int) *Object { return &a.objects[i/32][i%32] }
func (a *CaptureArena) text(i int) *string   { return &a.strings[i/32][i%32] }

// AllocCapture selects typed storage where it replaces a multi-field Object
// slice. Empty, single-field and all-reference structs keep existing storage.
func (s *Struct) AllocCapture(a *CaptureArena, l *CaptureLayout) {
	if len(l.slots) <= 1 || l.scalars == 0 {
		s.Alloc(len(l.slots))
		return
	}
	if a.nFields%16 == 0 {
		a.fields = append(a.fields, new([16]captureFields))
	}
	f := &a.fields[a.nFields/16][a.nFields%16]
	a.nFields++
	*f = captureFields{arena: a, layout: l, words: a.nWords, objects: a.nObjects, strings: a.nStrings}
	a.nWords += l.words
	a.nObjects += l.objects
	a.nStrings += l.strings
	for len(a.words)*128 < a.nWords {
		a.words = append(a.words, new([128]uint64))
	}
	for len(a.objects)*32 < a.nObjects {
		a.objects = append(a.objects, new([32]Object))
	}
	for len(a.strings)*32 < a.nStrings {
		a.strings = append(a.strings, new([32]string))
	}
	s.fields = f
}

func (f *captureFields) flag(slot, mask int) bool {
	return *f.arena.word(f.words + mask + slot/64)&(uint64(1)<<uint(slot%64)) != 0
}

func (f *captureFields) setFlag(slot, mask int, value bool) {
	p := f.arena.word(f.words + mask + slot/64)
	bit := uint64(1) << uint(slot%64)
	if value {
		*p |= bit
	} else {
		*p &^= bit
	}
}

func (f *captureFields) scalar(slot int) (Scalar, bool) {
	if obj := f.overrides[slot]; obj != nil {
		return objectScalar(*obj)
	}
	s := f.layout.slots[slot]
	if s.kind == ScalarObject {
		return Scalar{}, false
	}
	if !f.flag(slot, 0) {
		panic("unfilled captured field")
	}
	if !f.flag(slot, f.layout.mask) {
		return Scalar{Kind: ScalarNil}, true
	}
	v := Scalar{Kind: s.kind}
	switch s.kind {
	case ScalarString:
		v.Text = *f.arena.text(f.strings + s.offset)
	case ScalarBool, ScalarUint:
		v.Uint = *f.arena.word(f.words + s.offset)
	case ScalarInt:
		v.Int = int64(*f.arena.word(f.words + s.offset))
	case ScalarFloat32, ScalarFloat64:
		v.Real = math.Float64frombits(*f.arena.word(f.words + s.offset))
	case ScalarComplex64, ScalarComplex128:
		v.Real = math.Float64frombits(*f.arena.word(f.words + s.offset))
		v.Imag = math.Float64frombits(*f.arena.word(f.words + s.offset + 1))
	}
	return v, true
}

func (f *captureFields) put(slot int, v Scalar) {
	if obj := f.overrides[slot]; obj != nil {
		*obj = v.object()
		return
	}
	s := f.layout.slots[slot]
	if s.kind == ScalarObject || (v.Kind != ScalarNil && v.Kind != s.kind) {
		*f.field(slot) = v.object()
		return
	}
	f.setFlag(slot, 0, true)
	f.setFlag(slot, f.layout.mask, v.Kind != ScalarNil)
	if v.Kind == ScalarNil {
		if s.kind == ScalarString {
			*f.arena.text(f.strings + s.offset) = ""
		}
		return
	}
	switch v.Kind {
	case ScalarString:
		*f.arena.text(f.strings + s.offset) = v.Text
	case ScalarBool, ScalarUint:
		*f.arena.word(f.words + s.offset) = v.Uint
	case ScalarInt:
		*f.arena.word(f.words + s.offset) = uint64(v.Int)
	case ScalarFloat32, ScalarFloat64:
		*f.arena.word(f.words + s.offset) = math.Float64bits(v.Real)
	case ScalarComplex64, ScalarComplex128:
		*f.arena.word(f.words + s.offset) = math.Float64bits(v.Real)
		*f.arena.word(f.words + s.offset + 1) = math.Float64bits(v.Imag)
	}
}

func (f *captureFields) field(slot int) *Object {
	s := f.layout.slots[slot]
	if s.kind == ScalarObject {
		return f.arena.object(f.objects + s.offset)
	}
	if obj := f.overrides[slot]; obj != nil {
		return obj
	}
	obj := new(Object)
	if f.flag(slot, 0) {
		v, _ := f.scalar(slot)
		*obj = v.object()
	}
	if f.overrides == nil {
		f.overrides = make(map[int]*Object)
	}
	f.overrides[slot] = obj
	return obj
}

func (f *captureFields) save(w *Writer) {
	Uint(len(f.layout.slots)).save(w)
	for i, slot := range f.layout.slots {
		if obj := f.overrides[i]; obj != nil {
			Save(w, *obj)
		} else if slot.kind == ScalarObject {
			Save(w, *f.arena.object(f.objects + slot.offset))
		} else {
			v, _ := f.scalar(i)
			v.save(w)
		}
	}
}

func (*captureFields) load(r *Reader) Object {
	v := loadMultipleObjects(r)
	return &v
}

// StoreScalar captures a value without retaining a boxed primitive when typed
// storage is selected. Inline legacy storage keeps its ordinary representation.
func (s *Struct) StoreScalar(slot int, v Scalar) {
	if f, ok := s.fields.(*captureFields); ok {
		f.put(slot, v)
		return
	}
	*s.Field(slot) = v.object()
}

// Scalar reads either representation without reflective assignment.
func (s *Struct) Scalar(slot int) (Scalar, bool) {
	if f, ok := s.fields.(*captureFields); ok {
		return f.scalar(slot)
	}
	return objectScalar(*s.Field(slot))
}

func (v Scalar) save(w *Writer) {
	Uint(v.Kind).save(w)
	switch v.Kind {
	case ScalarNil:
	case ScalarBool, ScalarUint:
		Uint(v.Uint).save(w)
	case ScalarInt:
		Int(v.Int).save(w)
	case ScalarFloat32:
		Float32(v.Real).save(w)
	case ScalarFloat64:
		Float64(v.Real).save(w)
	case ScalarString:
		s := String(v.Text)
		s.save(w)
	case ScalarComplex64:
		c := Complex64(complex(v.Real, v.Imag))
		c.save(w)
	case ScalarComplex128:
		c := Complex128(complex(v.Real, v.Imag))
		c.save(w)
	default:
		panic("not a scalar")
	}
}

func (v Scalar) object() Object {
	switch v.Kind {
	case ScalarNil:
		return Nil{}
	case ScalarBool:
		return Bool(v.Uint == 1)
	case ScalarUint:
		return Uint(v.Uint)
	case ScalarInt:
		return Int(v.Int)
	case ScalarFloat32:
		return Float32(v.Real)
	case ScalarFloat64:
		return Float64(v.Real)
	case ScalarString:
		s := String(v.Text)
		return &s
	case ScalarComplex64:
		c := Complex64(complex(v.Real, v.Imag))
		return &c
	case ScalarComplex128:
		c := Complex128(complex(v.Real, v.Imag))
		return &c
	default:
		panic("not a scalar")
	}
}

func objectScalar(obj Object) (Scalar, bool) {
	switch v := obj.(type) {
	case Nil:
		return Scalar{Kind: ScalarNil}, true
	case Bool:
		var x uint64
		if v {
			x = 1
		}
		return Scalar{Kind: ScalarBool, Uint: x}, true
	case Uint:
		return Scalar{Kind: ScalarUint, Uint: uint64(v)}, true
	case Int:
		return Scalar{Kind: ScalarInt, Int: int64(v)}, true
	case Float32:
		return Scalar{Kind: ScalarFloat32, Real: float64(v)}, true
	case Float64:
		return Scalar{Kind: ScalarFloat64, Real: float64(v)}, true
	case *String:
		return Scalar{Kind: ScalarString, Text: string(*v)}, true
	case *Complex64:
		return Scalar{Kind: ScalarComplex64, Real: real(*v), Imag: imag(*v)}, true
	case *Complex128:
		return Scalar{Kind: ScalarComplex128, Real: real(*v), Imag: imag(*v)}, true
	default:
		return Scalar{}, false
	}
}

func loadScalar(r *Reader, kind uint64) Scalar {
	v := Scalar{Kind: kind}
	switch kind {
	case ScalarNil:
	case ScalarBool:
		if loadBool(r) {
			v.Uint = 1
		}
	case ScalarUint:
		v.Uint = uint64(loadUint(r))
	case ScalarInt:
		v.Int = int64(loadInt(r))
	case ScalarFloat32:
		v.Real = float64(loadFloat32(r))
	case ScalarFloat64:
		v.Real = float64(loadFloat64(r))
	case ScalarString:
		v.Text = string(loadString(r))
	case ScalarComplex64:
		c := loadComplex64(r)
		v.Real, v.Imag = real(c), imag(c)
	case ScalarComplex128:
		c := loadComplex128(r)
		v.Real, v.Imag = real(c), imag(c)
	default:
		panic("not a scalar")
	}
	return v
}
