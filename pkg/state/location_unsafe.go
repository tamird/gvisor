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

package state

import (
	"reflect"
	"unsafe"
)

// location is final, addressable Go storage with its exact decoding operations.
// The GC-visible address retains the allocation; decoder locations do not need
// the unaddressable snapshot identity used by the encoder.
type location struct {
	ops     *typeOperations
	address unsafe.Pointer
}

func reflectedLocation(obj reflect.Value) location {
	if !obj.CanAddr() {
		Failf("decode destination %v is not addressable", obj.Type())
	}
	return location{ops: operationsFor(obj.Type()), address: obj.Addr().UnsafePointer()}
}

func (obj location) reflect() reflect.Value {
	return reflect.NewAt(obj.ops.layout.typ, obj.address).Elem()
}

func (obj location) index(i int) location {
	if i < 0 || uintptr(i) >= obj.ops.layout.length {
		Failf("invalid array index %d in type %v", i, obj.ops.layout.typ)
	}
	return location{ops: obj.ops.elem, address: unsafe.Add(obj.address, uintptr(i)*obj.ops.elem.layout.size)}
}

func (obj location) incompatible() {
	Failf("wire value has incompatible destination type %v", obj.ops.layout.typ)
}
