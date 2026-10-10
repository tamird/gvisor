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
// it at registration or for reordered wire fields; wire does not register or
// reconcile native Go types.
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

// scalarKind reports the exact wire kind; nil remains distinct from zero.
func (f *captureFields) scalarKind(slot int) (uint64, bool) {
	if obj := f.overrides[slot]; obj != nil {
		return objectScalarKind(*obj)
	}
	kind := f.layout.slots[slot].kind
	if kind == ScalarObject {
		return 0, false
	}
	if !f.flag(slot, 0) {
		panic("unfilled captured field")
	}
	if !f.flag(slot, f.layout.mask) {
		return ScalarNil, true
	}
	return kind, true
}

// store returns an overriding Object slot when ordinary storage is required.
func (f *captureFields) store(slot int, kind uint64) *Object {
	if obj := f.overrides[slot]; obj != nil {
		return obj
	}
	s := f.layout.slots[slot]
	if s.kind == ScalarObject || (kind != ScalarNil && kind != s.kind) {
		return f.field(slot)
	}
	f.setFlag(slot, 0, true)
	f.setFlag(slot, f.layout.mask, kind != ScalarNil)
	if kind == ScalarNil && s.kind == ScalarString {
		*f.arena.text(f.strings + s.offset) = ""
	}
	return nil
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
		kind, _ := f.scalarKind(slot)
		switch kind {
		case ScalarNil:
			*obj = Nil{}
		case ScalarString:
			x := String(*f.arena.text(f.strings + s.offset))
			*obj = &x
		case ScalarComplex64, ScalarComplex128:
			x := complex(math.Float64frombits(*f.arena.word(f.words + s.offset)), math.Float64frombits(*f.arena.word(f.words + s.offset + 1)))
			if kind == ScalarComplex64 {
				v := Complex64(x)
				*obj = &v
			} else {
				v := Complex128(x)
				*obj = &v
			}
		default:
			*obj = wordObject(kind, *f.arena.word(f.words + s.offset))
		}
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
			continue
		}
		if slot.kind == ScalarObject {
			Save(w, *f.arena.object(f.objects + slot.offset))
			continue
		}
		kind, _ := f.scalarKind(i)
		Uint(kind).save(w)
		switch kind {
		case ScalarNil:
		case ScalarString:
			x := String(*f.arena.text(f.strings + slot.offset))
			x.save(w)
		case ScalarBool, ScalarUint:
			Uint(*f.arena.word(f.words + slot.offset)).save(w)
		case ScalarInt:
			Int(*f.arena.word(f.words + slot.offset)).save(w)
		case ScalarFloat32:
			Float32(math.Float64frombits(*f.arena.word(f.words + slot.offset))).save(w)
		case ScalarFloat64:
			Float64(math.Float64frombits(*f.arena.word(f.words + slot.offset))).save(w)
		case ScalarComplex64, ScalarComplex128:
			x := complex(math.Float64frombits(*f.arena.word(f.words + slot.offset)), math.Float64frombits(*f.arena.word(f.words + slot.offset + 1)))
			if kind == ScalarComplex64 {
				v := Complex64(x)
				v.save(w)
			} else {
				v := Complex128(x)
				v.save(w)
			}
		}
	}
}

func (*captureFields) load(r *Reader) Object {
	v := loadMultipleObjects(r)
	return &v
}

// StoreNil records the ordinary nil wire value without boxing it.
func (s *Struct) StoreNil(slot int) {
	if f, ok := s.fields.(*captureFields); ok && f.store(slot, ScalarNil) == nil {
		return
	}
	*s.Field(slot) = Nil{}
}

// StoreWord captures a numeric wire value. Floating-point words hold the bits
// of the widened float64 representation used by the existing wire decoder.
func (s *Struct) StoreWord(slot int, kind, bits uint64) {
	switch kind {
	case ScalarBool, ScalarInt, ScalarUint, ScalarFloat32, ScalarFloat64:
	default:
		panic("not a numeric scalar word")
	}
	if f, ok := s.fields.(*captureFields); ok && f.store(slot, kind) == nil {
		*f.arena.word(f.words + f.layout.slots[slot].offset) = bits
		return
	}
	*s.Field(slot) = wordObject(kind, bits)
}

// StoreString keeps strings in GC-visible storage.
func (s *Struct) StoreString(slot int, value string) {
	if f, ok := s.fields.(*captureFields); ok && f.store(slot, ScalarString) == nil {
		*f.arena.text(f.strings + f.layout.slots[slot].offset) = value
		return
	}
	v := String(value)
	*s.Field(slot) = &v
}

// StoreComplex preserves both full-width components until assignment checks.
func (s *Struct) StoreComplex(slot int, kind uint64, value complex128) {
	if kind != ScalarComplex64 && kind != ScalarComplex128 {
		panic("not a complex scalar")
	}
	if f, ok := s.fields.(*captureFields); ok && f.store(slot, kind) == nil {
		offset := f.words + f.layout.slots[slot].offset
		*f.arena.word(offset) = math.Float64bits(real(value))
		*f.arena.word(offset + 1) = math.Float64bits(imag(value))
		return
	}
	if kind == ScalarComplex64 {
		v := Complex64(value)
		*s.Field(slot) = &v
	} else {
		v := Complex128(value)
		*s.Field(slot) = &v
	}
}

// ScalarKind returns the actual primitive wire kind. Callers must use it before
// selecting ScalarWord, ScalarString or ScalarComplex; unexpected kinds retain
// the original Object assignment path.
func (s *Struct) ScalarKind(slot int) (uint64, bool) {
	if f, ok := s.fields.(*captureFields); ok {
		return f.scalarKind(slot)
	}
	return objectScalarKind(*s.Field(slot))
}

// ScalarWord reads a numeric value after ScalarKind validated its wire family.
func (s *Struct) ScalarWord(slot int) uint64 {
	if f, ok := s.fields.(*captureFields); ok && f.overrides[slot] == nil {
		return *f.arena.word(f.words + f.layout.slots[slot].offset)
	}
	switch v := (*s.Field(slot)).(type) {
	case Bool:
		if v {
			return 1
		}
		return 0
	case Int:
		return uint64(v)
	case Uint:
		return uint64(v)
	case Float32:
		return math.Float64bits(float64(v))
	case Float64:
		return math.Float64bits(float64(v))
	default:
		panic("not a numeric scalar")
	}
}

// ScalarString reads a value whose actual wire kind is ScalarString.
func (s *Struct) ScalarString(slot int) string {
	if f, ok := s.fields.(*captureFields); ok && f.overrides[slot] == nil {
		return *f.arena.text(f.strings + f.layout.slots[slot].offset)
	}
	return string(*(*s.Field(slot)).(*String))
}

// ScalarComplex reads a value whose actual wire kind is complex.
func (s *Struct) ScalarComplex(slot int) complex128 {
	if f, ok := s.fields.(*captureFields); ok && f.overrides[slot] == nil {
		offset := f.words + f.layout.slots[slot].offset
		return complex(math.Float64frombits(*f.arena.word(offset)), math.Float64frombits(*f.arena.word(offset + 1)))
	}
	switch v := (*s.Field(slot)).(type) {
	case *Complex64:
		return complex128(*v)
	case *Complex128:
		return complex128(*v)
	default:
		panic("not a complex scalar")
	}
}

func wordObject(kind, bits uint64) Object {
	switch kind {
	case ScalarBool:
		return Bool(bits == 1)
	case ScalarInt:
		return Int(bits)
	case ScalarUint:
		return Uint(bits)
	case ScalarFloat32:
		return Float32(math.Float64frombits(bits))
	case ScalarFloat64:
		return Float64(math.Float64frombits(bits))
	default:
		panic("not a numeric scalar")
	}
}

func objectScalarKind(obj Object) (uint64, bool) {
	switch obj.(type) {
	case Nil:
		return ScalarNil, true
	case Bool:
		return ScalarBool, true
	case Int:
		return ScalarInt, true
	case Uint:
		return ScalarUint, true
	case Float32:
		return ScalarFloat32, true
	case Float64:
		return ScalarFloat64, true
	case *String:
		return ScalarString, true
	case *Complex64:
		return ScalarComplex64, true
	case *Complex128:
		return ScalarComplex128, true
	default:
		return 0, false
	}
}

func loadCapturedField(r *Reader, s *Struct, slot int, kind uint64) {
	switch kind {
	case ScalarNil:
		s.StoreNil(slot)
	case ScalarBool:
		var value uint64
		if loadBool(r) {
			value = 1
		}
		s.StoreWord(slot, kind, value)
	case ScalarInt:
		s.StoreWord(slot, kind, uint64(loadInt(r)))
	case ScalarUint:
		s.StoreWord(slot, kind, uint64(loadUint(r)))
	case ScalarFloat32:
		s.StoreWord(slot, kind, math.Float64bits(float64(loadFloat32(r))))
	case ScalarFloat64:
		s.StoreWord(slot, kind, math.Float64bits(float64(loadFloat64(r))))
	case ScalarString:
		s.StoreString(slot, string(loadString(r)))
	case ScalarComplex64:
		s.StoreComplex(slot, kind, complex128(loadComplex64(r)))
	case ScalarComplex128:
		s.StoreComplex(slot, kind, complex128(loadComplex128(r)))
	default:
		panic("not a scalar wire kind")
	}
}
