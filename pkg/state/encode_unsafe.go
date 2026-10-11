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

package state

import (
	"reflect"
	"unsafe"

	"gvisor.dev/gvisor/pkg/state/wire"
)

// referenceArray owns a reusable pointer slot alongside the wire container.
// Keeping the slot here avoids allocating one scratch cell per element. The
// retained pointer to RefArray keeps this entire allocation reachable.
type referenceArray struct {
	wire.RefArray
	current unsafe.Pointer
}

// encodeReferenceArray captures an addressable array of pointers. The element
// kind is checked by encodeArray before borrowing its pointer-sized cells.
func (es *encodeState) encodeReferenceArray(obj reflect.Value, dest *wire.Object) {
	a := &referenceArray{RefArray: wire.RefArray{Contents: make([]wire.Ref, obj.Len())}}
	*dest = &a.RefArray
	// A pointer of the array's exact element type reads the reusable GC-visible
	// slot. resolve retains its pointee value, never this changing slot.
	value := reflect.NewAt(obj.Type().Elem(), unsafe.Pointer(&a.current)).Elem()
	pointers := unsafe.Slice((*unsafe.Pointer)(obj.Addr().UnsafePointer()), obj.Len())
	for i, pointer := range pointers {
		if pointer == nil {
			// The full pointer value is a zero Ref, including in a homogeneous
			// array whose first element is nil.
			continue
		}
		a.current = pointer
		es.resolve(value, &a.Contents[i])
	}
	a.current = nil
}

// arrayFromSlice constructs a new pointer to the slice data.
//
// It would be similar to the following:
//
//	x := make([]Foo, l, c)
//	a := ([l]Foo*)(unsafe.Pointer(x[0]))
func arrayFromSlice(obj reflect.Value) reflect.Value {
	arr := reflect.NewAt(
		reflect.ArrayOf(obj.Cap(), obj.Type().Elem()),
		unsafe.Pointer(obj.Pointer()))
	if obj.Len() < obj.Cap() {
		arr.Elem().Slice(obj.Len(), obj.Cap()).Clear()
	}
	return arr
}
