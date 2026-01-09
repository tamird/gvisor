// Copyright 2018 The gVisor Authors.
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

// Package ilist provides the implementation of intrusive linked lists.
package ilist

// Linker is the interface that objects must implement if they want to be added
// to and/or removed from List objects.
type Linker[Element any] interface {
	Next() Element
	Prev() Element
	SetNext(Element)
	SetPrev(Element)
}

// List is an intrusive list. Entries can be added to or removed from the list
// in O(1) time and with no additional memory allocations.
//
// The zero value for List is an empty list ready to use.
//
// To iterate over a list (where l is a List):
//
//	for e := l.Front(); e != nil; e = e.Next() {
//		// do something with e.
//	}
//
type List[Element interface {
	comparable
	Linker[Element]
}] struct {
	head Element
	tail Element
}

// Reset resets list l to the empty state.
func (l *List[Element]) Reset() {
	var zero Element
	l.head = zero
	l.tail = zero
}

// Empty returns true iff the list is empty.
//
//go:nosplit
func (l *List[Element]) Empty() bool {
	var zero Element
	return l.head == zero
}

// Front returns the first element of list l or nil.
//
//go:nosplit
func (l *List[Element]) Front() Element {
	return l.head
}

// Back returns the last element of list l or nil.
//
//go:nosplit
func (l *List[Element]) Back() Element {
	return l.tail
}

// Len returns the number of elements in the list.
//
// NOTE: This is an O(n) operation.
//
//go:nosplit
func (l *List[Element]) Len() (count int) {
	var zero Element
	for e := l.Front(); e != zero; e = e.Next() {
		count++
	}
	return count
}

// PushFront inserts the element e at the front of list l.
//
//go:nosplit
func (l *List[Element]) PushFront(e Element) {
	var zero Element
	e.SetNext(l.head)
	e.SetPrev(zero)
	if l.head != zero {
		l.head.SetPrev(e)
	} else {
		l.tail = e
	}

	l.head = e
}

// PushFrontList inserts list m at the start of list l, emptying m.
//
//go:nosplit
func (l *List[Element]) PushFrontList(m *List[Element]) {
	var zero Element
	if l.head == zero {
		l.head = m.head
		l.tail = m.tail
	} else if m.head != zero {
		l.head.SetPrev(m.tail)
		m.tail.SetNext(l.head)

		l.head = m.head
	}
	m.head = zero
	m.tail = zero
}

// PushBack inserts the element e at the back of list l.
//
//go:nosplit
func (l *List[Element]) PushBack(e Element) {
	var zero Element
	e.SetNext(zero)
	e.SetPrev(l.tail)
	if l.tail != zero {
		l.tail.SetNext(e)
	} else {
		l.head = e
	}

	l.tail = e
}

// PushBackList inserts list m at the end of list l, emptying m.
//
//go:nosplit
func (l *List[Element]) PushBackList(m *List[Element]) {
	var zero Element
	if l.head == zero {
		l.head = m.head
		l.tail = m.tail
	} else if m.head != zero {
		l.tail.SetNext(m.head)
		m.head.SetPrev(l.tail)

		l.tail = m.tail
	}
	m.head = zero
	m.tail = zero
}

// InsertAfter inserts e after b.
//
//go:nosplit
func (l *List[Element]) InsertAfter(b, e Element) {
	var zero Element
	a := b.Next()

	e.SetNext(a)
	e.SetPrev(b)
	b.SetNext(e)

	if a != zero {
		a.SetPrev(e)
	} else {
		l.tail = e
	}
}

// InsertBefore inserts e before a.
//
//go:nosplit
func (l *List[Element]) InsertBefore(a, e Element) {
	var zero Element
	b := a.Prev()
	e.SetNext(a)
	e.SetPrev(b)
	a.SetPrev(e)

	if b != zero {
		b.SetNext(e)
	} else {
		l.head = e
	}
}

// Remove removes e from l.
//
//go:nosplit
func (l *List[Element]) Remove(e Element) {
	var zero Element
	prev := e.Prev()
	next := e.Next()

	if prev != zero {
		prev.SetNext(next)
	} else if l.head == e {
		l.head = next
	}

	if next != zero {
		next.SetPrev(prev)
	} else if l.tail == e {
		l.tail = prev
	}

	e.SetNext(zero)
	e.SetPrev(zero)
}

// Entry is a default implementation of Linker. Users can add anonymous fields
// of this type to their structs to make them automatically implement the
// methods needed by List.
type Entry[Element any] struct {
	next Element
	prev Element
}

// Next returns the entry that follows e in the list.
//
//go:nosplit
func (e *Entry[Element]) Next() Element {
	return e.next
}

// Prev returns the entry that precedes e in the list.
//
//go:nosplit
func (e *Entry[Element]) Prev() Element {
	return e.prev
}

// SetNext assigns 'entry' as the entry that follows e in the list.
//
//go:nosplit
func (e *Entry[Element]) SetNext(next Element) {
	e.next = next
}

// SetPrev assigns 'entry' as the entry that precedes e in the list.
//
//go:nosplit
func (e *Entry[Element]) SetPrev(prev Element) {
	e.prev = prev
}
