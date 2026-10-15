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
// contain references that the graph resolver updates before emission. The
// parser reads its encoding as an ordinary struct Value, never another Record.
type Record struct {
	TypeID TypeID
	count  int
	fields RecordFields
}

// InitRecord initializes a fresh header owned by fields. Capture objects keep
// the header and their fields in one allocation, without promoting Record's
// methods onto each capture type. count must match fields.SaveFields's schema.
func InitRecord(r *Record, id TypeID, count int, fields RecordFields) *Record {
	if count < 0 {
		panic(fmt.Sprintf("negative record field count: %d", count))
	}
	r.TypeID = id
	r.count = count
	r.fields = fields
	return r
}

// RecordValue returns the capture storage selected for this record.
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
		prefix := slots.prefix(count)
		value := new(T)
		captureRecordSlots(prefix, P(value))
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
	record Record
	values []recordSlot
}

func (s *recordSlots) prefix(count int) []recordSlot {
	if count < 0 || count > len(s.values) {
		panic(fmt.Sprintf("forwarded field count %d exceeds record's %d slots", count, len(s.values)))
	}
	return s.values[:count]
}

func captureRecordSlots(slots []recordSlot, fields RecordFields) {
	for i := range slots {
		slots[i] = recordSlot{fields: fields, index: i}
	}
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
	s := &recordSlots{}
	InitRecord(&s.record, id, count, s)
	s.values = make([]recordSlot, count)
	return &s.record
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
