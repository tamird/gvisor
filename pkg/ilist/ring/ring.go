// Copyright 2023 The gVisor Authors.
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

// Package ring is an implementation of an intrusive circular linked list.
package ring

// Entry is an element in the circular linked list.
type Entry[Container any] struct {
	next      *Entry[Container]
	prev      *Entry[Container]
	container Container
}

// Init instantiates an Element to be an item in a ring (circularly-linked
// list).
//
//go:nosplit
func (e *Entry[Container]) Init(container Container) {
	e.next = e
	e.prev = e
	e.container = container
}

// Add adds new to old's ring.
//
//go:nosplit
func (e *Entry[Container]) Add(new *Entry[Container]) {
	next := e.next
	prev := e

	next.prev = new
	new.next = next
	new.prev = prev
	e.next = new
}

// Remove removes e from its ring and reinitializes it.
//
//go:nosplit
func (e *Entry[Container]) Remove() {
	next := e.next
	prev := e.prev

	next.prev = prev
	prev.next = next
	e.Init(e.container)
}

// Empty returns true if there are no other elements in the ring.
//
//go:nosplit
func (e *Entry[Container]) Empty() bool {
	return e.next == e
}

// Next returns the next containing object pointed to by the list.
//
//go:nosplit
func (e *Entry[Container]) Next() Container {
	return e.next.container
}

// Prev returns the previous containing object pointed to by the list.
//
//go:nosplit
func (e *Entry[Container]) Prev() Container {
	return e.prev.container
}
