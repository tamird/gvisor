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
	"encoding/binary"
	"io"
	"math"
)

// FrameArena owns captured field bytes and objects whose references cannot be
// serialized until graph discovery has finished. Offsets survive slice growth;
// object slots use fixed blocks so pointers returned by Field remain stable.
// This is an experimental representation of the existing state graph.
type FrameArena struct {
	data        []byte
	spans       []frameSpan
	objects     []*[64]Object
	objectSizes []int
	used        int
	frames      []*[64]frameFields
	frameCount  int
	writer      Writer
	counter     frameSizer
	countWriter Writer
}

// frameSpan selects literal bytes when end != 0, or an owned object slot when
// end == 0. An all-zero span is an unfilled field, not a serialized nil value.
type frameSpan struct {
	start int
	end   int
}

type frameFields struct {
	arena  *FrameArena
	first  int
	count  int
	frozen bool
	size   int
}

func (a *FrameArena) Write(p []byte) (int, error) {
	if uint64(len(a.data))+uint64(len(p)) > math.MaxInt {
		panic("framed state arena exceeds addressable memory")
	}
	a.data = append(a.data, p...)
	return len(p), nil
}

func (a *FrameArena) object(index int) *Object {
	index--
	return &a.objects[index/64][index%64]
}

func (a *FrameArena) newObject() int {
	if a.used == math.MaxInt {
		panic("too many framed state objects")
	}
	if a.used%64 == 0 {
		a.objects = append(a.objects, new([64]Object))
	}
	a.used++
	a.objectSizes = append(a.objectSizes, 0)
	return a.used
}

// AllocFrame selects length-delimited field snapshots for this struct. All
// structs in an encoding share one arena; there is no byte slice per field.
func (s *Struct) AllocFrame(arena *FrameArena, count int) {
	if count < 0 {
		panic("negative framed field count")
	}
	if arena.frameCount%64 == 0 {
		arena.frames = append(arena.frames, new([64]frameFields))
	}
	f := &arena.frames[arena.frameCount/64][arena.frameCount%64]
	*f = frameFields{arena: arena, first: len(arena.spans), count: count}
	arena.frameCount++
	arena.spans = append(arena.spans, make([]frameSpan, count)...)
	arena.writer.Writer = arena
	arena.countWriter.Writer = &arena.counter
	s.fields = f
}

// IsFramed reports whether this struct uses captured field spans.
func (s *Struct) IsFramed() bool {
	_, ok := s.fields.(*frameFields)
	return ok
}

// SnapshotScalar captures a primitive without boxing it as a wire.Object.
// References and composite values use Field until graph discovery finishes.
func (s *Struct) SnapshotScalar(slot int, value Scalar) {
	f := s.fields.(*frameFields)
	if f.frozen || slot < 0 || slot >= f.count {
		panic("invalid framed field capture")
	}
	a := f.arena
	start := len(a.data)
	w := &a.writer
	Uint(value.Kind).save(w)
	switch value.Kind {
	case ScalarNil:
	case ScalarBool, ScalarUint:
		Uint(value.Uint).save(w)
	case ScalarInt:
		Int(value.Int).save(w)
	case ScalarFloat32:
		Float32(value.Real).save(w)
	case ScalarFloat64:
		Float64(value.Real).save(w)
	case ScalarComplex64:
		v := Complex64(complex(value.Real, value.Imag))
		v.save(w)
	case ScalarComplex128:
		v := Complex128(complex(value.Real, value.Imag))
		v.save(w)
	case ScalarString:
		v := String(value.Text)
		v.save(w)
	default:
		panic("not a framed scalar")
	}
	a.spans[f.first+slot] = frameSpan{start: start, end: len(a.data)}
}

// frameReader reads a bounded subspan owned by the same arena. Nested frames
// keep subspan indices instead of copying their bytes into the arena again.
type frameReader struct {
	arena *FrameArena
	pos   int
	end   int
}

func (r *frameReader) Read(p []byte) (int, error) {
	if r.pos == r.end {
		return 0, io.EOF
	}
	n := copy(p, r.arena.data[r.pos:r.end])
	r.pos += n
	return n, nil
}

func (f *frameFields) field(slot int) *Object {
	if slot < 0 || slot >= f.count {
		panic("framed field index out of range")
	}
	a := f.arena
	span := a.spans[f.first+slot]
	if span.end != 0 {
		// The compatibility path materializes only the requested field. Direct
		// generated scalar loads use the bytes without constructing an Object.
		index := a.newObject()
		input := &frameReader{arena: a, pos: span.start, end: span.end}
		r := Reader{Reader: input, frames: a}
		*a.object(index) = Load(&r)
		if input.pos != input.end {
			panic("trailing bytes in framed field")
		}
		span = frameSpan{start: index}
	} else if span.start == 0 {
		span.start = a.newObject()
	}
	a.spans[f.first+slot] = span
	f.frozen = false
	return a.object(span.start)
}

// FrameBytes returns the immutable bytes of a captured field, if it has not
// already been materialized through the compatibility field interface.
func (s *Struct) FrameBytes(slot int) ([]byte, bool) {
	f, ok := s.fields.(*frameFields)
	if !ok {
		return nil, false
	}
	if slot < 0 || slot >= f.count {
		panic("framed field index out of range")
	}
	span := f.arena.spans[f.first+slot]
	if span.end == 0 {
		return nil, false
	}
	return f.arena.data[span.start:span.end], true
}

// Finish freezes remaining owned values after graph discovery has resolved all
// references. It never calls user hooks or reads the original application.
func (a *FrameArena) Finish() {
	for i := 0; i < a.frameCount; i++ {
		a.frames[i/64][i%64].finish()
	}
}

// frameSizer counts the existing wire encoding without retaining a second
// encoded copy. Completed child frames contribute their cached sizes.
type frameSizer struct{ size int }

func (s *frameSizer) Write(p []byte) (int, error) {
	s.add(len(p))
	return len(p), nil
}

func (s *frameSizer) add(n int) {
	if n > math.MaxInt-s.size {
		panic("framed state size overflow")
	}
	s.size += n
}

func uintSize(x uint64) int {
	n := 1
	for x >= 128 {
		x >>= 7
		n++
	}
	return n
}

func (f *frameFields) finish() {
	if f.frozen {
		return
	}
	a := f.arena
	size := uintSize(uint64(f.count))
	for i := 0; i < f.count; i++ {
		span := a.spans[f.first+i]
		n := span.end - span.start
		if span.end == 0 {
			if span.start == 0 {
				panic("unfilled framed field")
			}
			value := *a.object(span.start)
			freezeFrames(value)
			a.counter.size = 0
			Save(&a.countWriter, value)
			n = a.counter.size
			a.objectSizes[span.start-1] = n
		}
		if n > math.MaxInt-uintSize(uint64(n))-size {
			panic("framed state size overflow")
		}
		size += uintSize(uint64(n)) + n
	}
	f.size = size
	f.frozen = true
}

// freezeFrames visits only captured composite values. Pointer references are
// leaves, so cycles in the application graph do not recurse here.
func freezeFrames(value Object) {
	switch x := value.(type) {
	case *Struct:
		if f, ok := x.fields.(*frameFields); ok {
			f.finish()
		} else {
			for i := 0; i < x.Fields(); i++ {
				freezeFrames(*x.Field(i))
			}
		}
	case *Array:
		for _, element := range x.Contents {
			freezeFrames(element)
		}
	case *Map:
		for i := range x.Keys {
			freezeFrames(x.Keys[i])
			freezeFrames(x.Values[i])
		}
	case *Interface:
		freezeFrames(x.Value)
	}
}

func (f *frameFields) save(w *Writer) {
	if !f.frozen {
		panic("framed state emitted before reference resolution")
	}
	if counter, ok := w.Writer.(*frameSizer); ok {
		counter.add(f.size)
		return
	}
	Uint(f.count).save(w)
	for i := 0; i < f.count; i++ {
		span := f.arena.spans[f.first+i]
		if span.end == 0 {
			Uint(f.arena.objectSizes[span.start-1]).save(w)
			Save(w, *f.arena.object(span.start))
		} else {
			Uint(span.end - span.start).save(w)
			if _, err := w.Write(f.arena.data[span.start:span.end]); err != nil {
				panic(err)
			}
		}
	}
}

func (*frameFields) load(*Reader) Object {
	panic("framed fields require a containing struct")
}

func loadFramedStruct(r *Reader) Struct {
	typeID := TypeID(loadUint(r))
	count := loadUint(r)
	if uint64(count) > uint64(math.MaxInt) {
		panic("too many framed fields")
	}
	if r.frames == nil {
		r.frames = new(FrameArena)
	}
	var s Struct
	s.TypeID = typeID
	s.AllocFrame(r.frames, int(count))
	f := s.fields.(*frameFields)
	for i := 0; i < f.count; i++ {
		size := loadUint(r)
		if size == 0 || uint64(size) > math.MaxInt {
			panic("invalid framed field length")
		}
		if input, ok := r.Reader.(*frameReader); ok && input.arena == r.frames {
			if uint64(size) > uint64(input.end-input.pos) {
				panic(io.ErrUnexpectedEOF)
			}
			start := input.pos
			input.pos += int(size)
			r.frames.spans[f.first+i] = frameSpan{start: start, end: input.pos}
		} else {
			if uint64(len(r.frames.data))+uint64(size) > math.MaxInt {
				panic("framed state arena exceeds addressable memory")
			}
			start := len(r.frames.data)
			r.frames.data = append(r.frames.data, make([]byte, int(size))...)
			readFull(r, r.frames.data[start:])
			r.frames.spans[f.first+i] = frameSpan{start: start, end: len(r.frames.data)}
		}
	}
	f.size = uintSize(uint64(f.count))
	for i := 0; i < f.count; i++ {
		span := r.frames.spans[f.first+i]
		n := span.end - span.start
		f.size += uintSize(uint64(n)) + n
	}
	f.frozen = true
	return s
}

// Scalar is a transient decoded primitive, not a retained wire Object. Kind is
// one of the existing wire tags; named-type assignment remains the state
// decoder's responsibility. Numeric values retain the original wire width.
type Scalar struct {
	Kind uint64
	Int  int64
	Uint uint64
	Real float64
	Imag float64
	Text string
}

// Primitive tags are exported for direct generated stores. Their values are
// the original wire tags, not a second scalar encoding.
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
)

type frameCursor struct {
	data []byte
}

func (c *frameCursor) uint() uint64 {
	x, n := binary.Uvarint(c.data)
	if n == 0 {
		panic(io.ErrUnexpectedEOF)
	}
	if n < 0 {
		panic("overflow")
	}
	c.data = c.data[n:]
	return x
}

// Scalar reads a captured primitive without constructing a wire.Object. It
// returns false for composite fields and the caller uses the ordinary decoder.
func (s *Struct) Scalar(slot int) (Scalar, bool) {
	data, ok := s.FrameBytes(slot)
	if !ok {
		return Scalar{}, false
	}
	c := frameCursor{data: data}
	value := Scalar{Kind: c.uint()}
	switch value.Kind {
	case ScalarNil:
	case ScalarBool, ScalarUint:
		value.Uint = c.uint()
	case ScalarInt:
		x := c.uint()
		value.Int = int64(x >> 1)
		if x&1 != 0 {
			value.Int = ^value.Int
		}
	case ScalarFloat32:
		value.Real = float64(math.Float32frombits(uint32(c.uint())))
	case ScalarFloat64:
		value.Real = math.Float64frombits(c.uint())
	case ScalarComplex64:
		value.Real = float64(math.Float32frombits(uint32(c.uint())))
		value.Imag = float64(math.Float32frombits(uint32(c.uint())))
	case ScalarComplex128:
		value.Real = math.Float64frombits(c.uint())
		value.Imag = math.Float64frombits(c.uint())
	case ScalarString:
		n := c.uint()
		if n > uint64(len(c.data)) {
			panic(io.ErrUnexpectedEOF)
		}
		// The restored string must not retain the complete encoded arena.
		value.Text = string(c.data[:n])
		c.data = c.data[n:]
	default:
		return Scalar{}, false
	}
	if len(c.data) != 0 {
		panic("trailing bytes in framed scalar")
	}
	return value, true
}
