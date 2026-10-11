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

import "fmt"

// RecordFields emits [first, last) in declared order, including each Object's
// type. Full records and compatibility slots share this emitter. It must not
// consult the original object or invoke save hooks again.
type RecordFields interface {
	SaveFields(w *Writer, first, last int)
}

// Record is a captured struct, owned by an in-progress save. Its fields may
// contain references that the graph resolver updates before emission. Loading
// a Record produces an ordinary mutable Struct, never another Record.
type Record struct {
	TypeID TypeID
	count  int
	fields RecordFields
}

type typedRecord[T any] struct {
	Record
	value T
}

// NewRecord allocates the header and its captured fields together. The caller
// must use the field count from the same schema as T.SaveFields.
func NewRecord[T any, P interface {
	*T
	RecordFields
}](id TypeID, count int) *Record {
	if count < 0 {
		panic(fmt.Sprintf("negative record field count: %d", count))
	}
	r := &typedRecord[T]{}
	r.TypeID = id
	r.count = count
	r.fields = P(&r.value)
	return &r.Record
}

// RecordValue returns the capture storage selected by the registered factory.
// Keeping the exact type check here prevents a saver promoted from an embedded
// type from interpreting another type's record as its own.
func RecordValue[T any](r *Record) *T {
	return any(r.fields).(*T)
}

// CaptureRecordValue returns the generated capture storage. When a manual
// saver forwards to a generated saver, only that saver's prefix slots are
// replaced. Other slots and subsequent ordinary saves remain composable.
func CaptureRecordValue[T any, P interface {
	*T
	RecordFields
}](r *Record, count int) *T {
	if slots, ok := r.fields.(*recordSlots); ok {
		if count < 0 || count > len(slots.values) {
			panic(fmt.Sprintf("forwarded field count %d exceeds record's %d slots", count, len(slots.values)))
		}
		value := new(T)
		for i := range count {
			slots.values[i] = recordSlot{fields: P(value), index: i}
		}
		return value
	}
	return RecordValue[T](r)
}

// A compatibility slot contains either an ordinary capture or one field of a
// generated capture. Both are retained until the single graph is resolved.
type recordSlot struct {
	value  Object
	fields RecordFields
	index  int
}

type recordSlots struct {
	values []recordSlot
}

func (s *recordSlots) SaveFields(w *Writer, first, last int) {
	for _, slot := range s.values[first:last] {
		if slot.fields != nil {
			slot.fields.SaveFields(w, slot.index, slot.index+1)
		} else {
			Save(w, slot.value)
		}
	}
}

// NewSlotRecord adapts manually implemented StateSave methods to the same
// capture graph and emitter used by generated savers.
func NewSlotRecord(id TypeID, count int) *Record {
	r := NewRecord[recordSlots](id, count)
	RecordValue[recordSlots](r).values = make([]recordSlot, count)
	return r
}

// Field returns a compatibility slot, replacing any earlier forwarded field.
func (r *Record) Field(slot int) *Object {
	value := &RecordValue[recordSlots](r).values[slot]
	value.fields = nil
	return &value.value
}

func (r *Record) save(w *Writer) {
	Uint(r.TypeID).save(w)
	switch r.count {
	case 0:
		typeNoObjects.save(w)
	case 1:
	default:
		typeMultipleObjects.save(w)
		Uint(r.count).save(w)
	}
	r.fields.SaveFields(w, 0, r.count)
}

func (*Record) load(r *Reader) Object {
	s := loadStruct(r)
	return &s
}
