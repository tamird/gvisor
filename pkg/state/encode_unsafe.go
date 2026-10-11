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

// savedPointer gives the compiler the pointer's actual saver implementation.
// Defined pointer types without this method set keep the dynamic path.
type savedPointer[T any] interface {
	*T
	SaverLoader
}

// SavePointer captures a pointer into an ordinary Sink field. It does not
// allocate a typed record for a type containing only graph-bearing fields.
func SavePointer[T any, P savedPointer[T]](s Sink, slot int, ptr P) {
	s.internal.encoded.AllocIfNeeded(len(s.internal.typ.Fields))
	CapturePointer(s, ptr, s.internal.encoded.Field(slot))
}

// CapturePointer captures a pointer into a stable child slot. The region owns
// ptr through an interface, independently of its integer address lookup key.
func CapturePointer[T any, P savedPointer[T]](s Sink, ptr P, dest *wire.Object) {
	if ptr == nil {
		*dest = wire.Nil{}
		return
	}
	es := s.internal.es
	te := s.internal.typ
	if te.nativeType != reflect.TypeFor[T]() {
		te = es.types.native(reflect.TypeFor[T]())
	}
	if te.nativeKind == reflect.Struct && te.capture == nil {
		// This is a static function value shared by all regions of this type,
		// not a closure bound to the object being captured.
		te.capture = captureStructPointer[T, P]
	}
	ref := new(wire.Ref)
	*dest = ref
	es.resolveRegion(ptr, te, uintptr(unsafe.Pointer(ptr)), encodeDefault, ref)
}

func captureStructPointer[T any, P savedPointer[T]](es *encodeState, owner any, dest *wire.Object) {
	ptr := owner.(P)
	// The existing cache still uses the original addressable reflect.Value as
	// its identity token. No addressability copy or hook precedes this lookup.
	es.encodeStructValue(reflect.ValueOf(ptr).Elem(), ptr, dest)
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
