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

// Capture owns legacy wire bytes recorded before graph references settle.
// Child objects remain GC-visible and are emitted only after graph discovery.
// A capture is private to one state Save; it is not a new stream format.
type Capture struct {
	data   []byte
	writer Writer
}

// NewCapture returns storage for one graph capture.
func NewCapture() *Capture {
	c := &Capture{}
	c.writer.Writer = c
	return c
}

// Write implements io.Writer for the primitive wire encoders.
func (c *Capture) Write(p []byte) (int, error) {
	c.data = append(c.data, p...)
	return len(p), nil
}

type capturePiece struct {
	start, end int
	object     Object
}

type captureFields struct {
	capture *Capture
	count   int
	next    int
	pieces  []capturePiece
}

// AllocCapture selects state-owned, ordered capture storage. Slots must be
// supplied exactly once in increasing order through Capture methods. Field
// pointers are deliberately unavailable: references are owned by child objects.
// Ordinary Alloc and all decoded Struct values retain mutable Field semantics.
func (s *Struct) AllocCapture(c *Capture, count int) {
	if count < 0 {
		panic("negative capture field count")
	}
	s.fields = &captureFields{capture: c, count: count}
}

// IsCapture reports whether this is an ordered state-owned snapshot.
func (s *Struct) IsCapture() bool {
	_, ok := s.fields.(*captureFields)
	return ok
}

func (f *captureFields) begin(slot int) int {
	if slot != f.next || slot >= f.count {
		panic("capture fields saved out of order")
	}
	f.next++
	return len(f.capture.data)
}

func (f *captureFields) end(start int) {
	end := len(f.capture.data)
	if n := len(f.pieces); n > 0 && f.pieces[n-1].object == nil && f.pieces[n-1].end == start {
		f.pieces[n-1].end = end
	} else {
		f.pieces = append(f.pieces, capturePiece{start: start, end: end})
	}
}

// CaptureObject retains a child snapshot or reference, without emitting it or
// copying its bytes. Graph resolution may still update the referenced object.
func (s *Struct) CaptureObject(slot int, object Object) {
	f := s.fields.(*captureFields)
	f.begin(slot)
	if object == nil {
		panic("nil capture object")
	}
	f.pieces = append(f.pieces, capturePiece{object: object})
}

func (f *captureFields) save(w *Writer) {
	if f.next != f.count {
		panic("incomplete capture")
	}
	switch f.count {
	case 0:
		typeNoObjects.save(w)
	case 1:
	default:
		typeMultipleObjects.save(w)
		Uint(f.count).save(w)
	}
	for _, piece := range f.pieces {
		if piece.object != nil {
			Save(w, piece.object)
		} else if _, err := w.Write(f.capture.data[piece.start:piece.end]); err != nil {
			panic(err)
		}
	}
}

func (*captureFields) load(r *Reader) Object { return Load(r) }

// CaptureBool snapshots a primitive using the existing zero-value encoding.
func (s *Struct) CaptureBool(slot int, value bool) {
	f := s.fields.(*captureFields)
	start := f.begin(slot)
	w := &f.capture.writer
	if !value {
		typeNil.save(w)
	} else {
		typeBool.save(w)
		Bool(value).save(w)
	}
	f.end(start)
}

// CaptureInt snapshots a primitive using the existing zero-value encoding.
func (s *Struct) CaptureInt(slot int, value int64) {
	f := s.fields.(*captureFields)
	start := f.begin(slot)
	w := &f.capture.writer
	if value == 0 {
		typeNil.save(w)
	} else {
		typeInt.save(w)
		Int(value).save(w)
	}
	f.end(start)
}

// CaptureUint snapshots a primitive using the existing zero-value encoding.
func (s *Struct) CaptureUint(slot int, value uint64) {
	f := s.fields.(*captureFields)
	start := f.begin(slot)
	w := &f.capture.writer
	if value == 0 {
		typeNil.save(w)
	} else {
		typeUint.save(w)
		Uint(value).save(w)
	}
	f.end(start)
}

// CaptureFloat32 snapshots a primitive using the existing zero-value encoding.
func (s *Struct) CaptureFloat32(slot int, value float64) {
	f := s.fields.(*captureFields)
	start := f.begin(slot)
	w := &f.capture.writer
	if value == 0 {
		typeNil.save(w)
	} else {
		typeFloat32.save(w)
		Float32(value).save(w)
	}
	f.end(start)
}

// CaptureFloat64 snapshots a primitive using the existing zero-value encoding.
func (s *Struct) CaptureFloat64(slot int, value float64) {
	f := s.fields.(*captureFields)
	start := f.begin(slot)
	w := &f.capture.writer
	if value == 0 {
		typeNil.save(w)
	} else {
		typeFloat64.save(w)
		Float64(value).save(w)
	}
	f.end(start)
}

// CaptureComplex64 snapshots a primitive using the existing zero-value encoding.
func (s *Struct) CaptureComplex64(slot int, value complex128) {
	f := s.fields.(*captureFields)
	start := f.begin(slot)
	w := &f.capture.writer
	if value == 0 {
		typeNil.save(w)
	} else {
		typeComplex64.save(w)
		x := Complex64(value)
		x.save(w)
	}
	f.end(start)
}

// CaptureComplex128 snapshots a primitive using the existing zero-value encoding.
func (s *Struct) CaptureComplex128(slot int, value complex128) {
	f := s.fields.(*captureFields)
	start := f.begin(slot)
	w := &f.capture.writer
	if value == 0 {
		typeNil.save(w)
	} else {
		typeComplex128.save(w)
		x := Complex128(value)
		x.save(w)
	}
	f.end(start)
}

// CaptureString snapshots a primitive using the existing zero-value encoding.
func (s *Struct) CaptureString(slot int, value string) {
	f := s.fields.(*captureFields)
	start := f.begin(slot)
	w := &f.capture.writer
	if value == "" {
		typeNil.save(w)
	} else {
		typeString.save(w)
		x := String(value)
		x.save(w)
	}
	f.end(start)
}
