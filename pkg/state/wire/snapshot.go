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

// typedFieldSnapshot owns a fixed record; its emitter writes the ordinary field
// grammar. It is used only for immutable state-owned snapshots. Loaded structs
// retain their ordinary mutable Object slots.
type typedFieldSnapshot interface {
	Object
	fieldCount() int
}

type typedFields[T any] struct {
	value T
	count int
	emit  func(*Writer, *T)
}

// AllocSnapshot installs a typed record and returns its stable capture storage.
// Once capture completes, neither the record nor its child handles may be
// changed except by the owning graph resolver before emission. The emitter must
// write exactly count ordinary Objects, without a containing struct header.
// The struct must not already own ordinary fields or another snapshot.
func AllocSnapshot[T any](s *Struct, count int, emit func(*Writer, *T)) *T {
	if count < 0 || emit == nil {
		panic("invalid typed snapshot")
	}
	if s.fields != nil {
		panic("typed snapshot cannot replace existing fields")
	}
	fields := &typedFields[T]{count: count, emit: emit}
	s.fields = fields
	return &fields.value
}

func (f *typedFields[T]) fieldCount() int { return f.count }

func (f *typedFields[T]) save(w *Writer) {
	switch f.count {
	case 0:
		typeNoObjects.save(w)
	case 1:
	default:
		typeMultipleObjects.save(w)
		Uint(f.count).save(w)
	}
	f.emit(w, &f.value)
}

func (*typedFields[T]) load(r *Reader) Object { return Load(r) }

// SaveIntField writes a saved signed field without constructing an Object.
func SaveIntField(w *Writer, value Int) {
	if value == 0 {
		typeNil.save(w)
	} else {
		typeInt.save(w)
		Int(value).save(w)
	}
}

// SaveStringField writes a saved string without constructing an Object.
func SaveStringField(w *Writer, value String) {
	if value == "" {
		typeNil.save(w)
	} else {
		typeString.save(w)
		text := String(value)
		text.save(w)
	}
}

// SaveUintField writes a saved unsigned field without constructing an Object.
func SaveUintField(w *Writer, value Uint) {
	if value == 0 {
		typeNil.save(w)
	} else {
		typeUint.save(w)
		Uint(value).save(w)
	}
}

// SaveBoolField writes a saved boolean field without constructing an Object.
func SaveBoolField(w *Writer, value Bool) {
	if !value {
		typeNil.save(w)
	} else {
		typeBool.save(w)
		Bool(value).save(w)
	}
}

// SaveFloat32Field preserves the existing Float32 encoding after capture's
// float64 promotion, including the signaling-NaN conversion boundary.
func SaveFloat32Field(w *Writer, value Float64) {
	if value == 0 {
		typeNil.save(w)
	} else {
		typeFloat32.save(w)
		Float32(value).save(w)
	}
}

// SaveFloat64Field writes a saved float64 without constructing an Object.
func SaveFloat64Field(w *Writer, value Float64) {
	if value == 0 {
		typeNil.save(w)
	} else {
		typeFloat64.save(w)
		value.save(w)
	}
}

// SaveComplex64Field preserves the existing complex128 promotion boundary.
func SaveComplex64Field(w *Writer, value Complex128) {
	if value == 0 {
		typeNil.save(w)
	} else {
		typeComplex64.save(w)
		narrow := Complex64(value)
		narrow.save(w)
	}
}

// SaveComplex128Field writes a saved complex128 without constructing an Object.
func SaveComplex128Field(w *Writer, value Complex128) {
	if value == 0 {
		typeNil.save(w)
	} else {
		typeComplex128.save(w)
		value.save(w)
	}
}
