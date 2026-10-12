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

// value combines exact type operations with GC-visible storage. reflected is
// retained at adapter boundaries: an unaddressable map entry's snapshot key is
// distinct from the addressable copy on which its save hook runs. In particular,
// reconstructing that key with NewAt would change the established hook identity.
type value struct {
	ops       *typeOperations
	address   unsafe.Pointer
	reflected reflect.Value
}

// copyValue gives a value snapshot separate addressable storage. The caller
// retains the original reflected identity when it is used as a capture key.
func copyValue(source reflect.Value) reflect.Value {
	copy := reflect.New(source.Type()).Elem()
	copy.Set(source)
	return copy
}

func valueAt(address unsafe.Pointer, ops *typeOperations) value {
	return value{ops: ops, address: address}
}

func reflectedValue(obj reflect.Value) value {
	v := value{ops: operationsFor(obj.Type()), reflected: obj}
	if obj.CanAddr() {
		v.address = obj.Addr().UnsafePointer()
	}
	return v
}

func (v value) reflect() reflect.Value {
	if v.reflected.IsValid() {
		return v.reflected
	}
	return reflect.NewAt(v.ops.layout.typ, v.address).Elem()
}

func (v value) index(i int) value {
	child := value{ops: v.ops.elem}
	if v.address != nil {
		child.address = unsafe.Add(v.address, uintptr(i)*child.ops.layout.size)
	}
	// Only struct snapshots have reflected identity. Preserve the original
	// field-derived flags through containing arrays instead of rebuilding a
	// different key. Primitive and pointer elements need no reflected view.
	if v.reflected.IsValid() && (child.ops.layout.kind == reflect.Struct || child.ops.layout.kind == reflect.Array || child.address == nil) {
		child.reflected = v.reflected.Index(i)
	}
	return child
}

func (v value) pointer() unsafe.Pointer {
	if v.address != nil {
		return *(*unsafe.Pointer)(v.address)
	}
	return v.reflected.UnsafePointer()
}

func (v value) signed() int64 {
	if v.address == nil {
		return v.reflected.Int()
	}
	switch v.ops.layout.kind {
	case reflect.Int:
		return int64(*(*int)(v.address))
	case reflect.Int8:
		return int64(*(*int8)(v.address))
	case reflect.Int16:
		return int64(*(*int16)(v.address))
	case reflect.Int32:
		return int64(*(*int32)(v.address))
	default:
		return *(*int64)(v.address)
	}
}

func (v value) unsigned() uint64 {
	if v.address == nil {
		return v.reflected.Uint()
	}
	switch v.ops.layout.kind {
	case reflect.Uint:
		return uint64(*(*uint)(v.address))
	case reflect.Uint8:
		return uint64(*(*uint8)(v.address))
	case reflect.Uint16:
		return uint64(*(*uint16)(v.address))
	case reflect.Uint32:
		return uint64(*(*uint32)(v.address))
	case reflect.Uintptr:
		return uint64(*(*uintptr)(v.address))
	default:
		return *(*uint64)(v.address)
	}
}

func (v value) boolean() bool {
	if v.address == nil {
		return v.reflected.Bool()
	}
	return *(*bool)(v.address)
}

func (v value) floating() float64 {
	if v.address == nil {
		return v.reflected.Float()
	}
	if v.ops.layout.kind == reflect.Float32 {
		return float64(*(*float32)(v.address))
	}
	return *(*float64)(v.address)
}

func (v value) complex() complex128 {
	if v.address == nil {
		return v.reflected.Complex()
	}
	if v.ops.layout.kind == reflect.Complex64 {
		return complex128(*(*complex64)(v.address))
	}
	return *(*complex128)(v.address)
}

func (v value) text() string {
	if v.address == nil {
		return v.reflected.String()
	}
	return *(*string)(v.address)
}

func (v value) zero() bool {
	switch v.ops.layout.kind {
	case reflect.Ptr:
		return v.pointer() == nil
	case reflect.Bool:
		return !v.boolean()
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return v.signed() == 0
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
		return v.unsigned() == 0
	case reflect.Float32, reflect.Float64:
		return v.floating() == 0
	case reflect.Complex64, reflect.Complex128:
		return v.complex() == 0
	case reflect.String:
		return v.text() == ""
	case reflect.Array:
		for i := 0; i < int(v.ops.layout.length); i++ {
			if !v.index(i).zero() {
				return false
			}
		}
		return true
	default:
		return v.reflect().IsZero()
	}
}

func (v value) require(kinds ...reflect.Kind) {
	for _, kind := range kinds {
		if v.ops.layout.kind == kind {
			return
		}
	}
	Failf("wire value has incompatible destination type %v", v.ops.layout.typ)
}

func (v value) setSigned(x int64) {
	v.require(reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64)
	switch v.ops.layout.kind {
	case reflect.Int:
		*(*int)(v.address) = int(x)
	case reflect.Int8:
		*(*int8)(v.address) = int8(x)
	case reflect.Int16:
		*(*int16)(v.address) = int16(x)
	case reflect.Int32:
		*(*int32)(v.address) = int32(x)
	case reflect.Int64:
		*(*int64)(v.address) = x
	}
	if got := v.signed(); got != x {
		Failf("signed integer truncated from %v to %v", x, got)
	}
}

func (v value) setUnsigned(x uint64) {
	v.require(reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr)
	switch v.ops.layout.kind {
	case reflect.Uint:
		*(*uint)(v.address) = uint(x)
	case reflect.Uint8:
		*(*uint8)(v.address) = uint8(x)
	case reflect.Uint16:
		*(*uint16)(v.address) = uint16(x)
	case reflect.Uint32:
		*(*uint32)(v.address) = uint32(x)
	case reflect.Uint64:
		*(*uint64)(v.address) = x
	case reflect.Uintptr:
		*(*uintptr)(v.address) = uintptr(x)
	}
	if got := v.unsigned(); got != x {
		Failf("unsigned integer truncated from %v to %v", x, got)
	}
}

func (v value) setFloat(x float64, check bool) {
	v.require(reflect.Float32, reflect.Float64)
	if v.ops.layout.kind == reflect.Float32 {
		*(*float32)(v.address) = float32(x)
	} else {
		*(*float64)(v.address) = x
	}
	if check && !isFloatEq(v.floating(), x) {
		Failf("floating point number truncated from %v to %v", x, v.floating())
	}
}

func (v value) setComplex(x complex128, check bool) {
	v.require(reflect.Complex64, reflect.Complex128)
	if v.ops.layout.kind == reflect.Complex64 {
		*(*complex64)(v.address) = complex64(x)
	} else {
		*(*complex128)(v.address) = x
	}
	if check && !isComplexEq(v.complex(), x) {
		Failf("complex number truncated from %v to %v", x, v.complex())
	}
}
