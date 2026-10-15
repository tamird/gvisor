// Copyright 2020 The gVisor Authors.
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
	"fmt"
	"reflect"
	"unsafe"
)

// reflectValueRWAddr is equivalent to obj.Addr(), except that the returned
// reflect.Value is usable in assignments even if obj was obtained by the use
// of unexported struct fields.
//
// Preconditions: obj.CanAddr().
func reflectValueRWAddr(obj reflect.Value) reflect.Value {
	return reflect.NewAt(obj.Type(), unsafe.Pointer(obj.UnsafeAddr()))
}

// reflectValueRWSlice3 is equivalent to arr.Slice3(i, j, k), except that the
// returned reflect.Value is usable in assignments even if obj was obtained by
// the use of unexported struct fields.
//
// Preconditions:
//   - arr.Kind() == reflect.Array and arr.CanAddr().
//   - i, j, k >= 0.
//   - i <= j <= k <= arr.Len().
func reflectValueRWSlice3(arr reflect.Value, i, j, k int) reflect.Value {
	if arr.Kind() != reflect.Array {
		panic(fmt.Sprintf("arr has kind %v, wanted %v", arr.Kind(), reflect.Array))
	}
	return reflectValueRWAddr(arr).Elem().Slice3(i, j, k)
}

// restoreDestination is a view of real typed storage. pointer remains a Go
// pointer, including for interior fields/elements; it is never kept in uintptr.
// A reflected token is retained only when its read-only/addressability flags
// matter. Reconstructing such values with NewAt would erase those flags.
type restoreDestination struct {
	plan      *restorePlan
	pointer   unsafe.Pointer
	reflected reflect.Value
}

func (ods *objectDecodeState) destination() restoreDestination {
	return restoreDestination{plan: ods.plan, pointer: reflect.ValueOf(ods.pointer).UnsafePointer()}
}

func (ds *decodeState) destination(value reflect.Value) restoreDestination {
	if !value.IsValid() {
		return restoreDestination{}
	}
	plan := ds.plan(value.Type())
	if !value.CanAddr() || !value.CanSet() {
		return restoreDestination{plan: plan, reflected: value}
	}
	return restoreDestination{plan: plan, pointer: unsafe.Pointer(value.UnsafeAddr())}
}

func (ds *decodeState) pointerDestination(pointer reflect.Value) restoreDestination {
	if pointer.Kind() != reflect.Pointer {
		return ds.destination(pointer.Elem())
	}
	return restoreDestination{plan: ds.plan(pointer.Type().Elem()), pointer: pointer.UnsafePointer()}
}

func (target restoreDestination) value() reflect.Value {
	if target.reflected.IsValid() || target.plan == nil {
		return target.reflected
	}
	return reflect.NewAt(target.plan.typ, target.pointer).Elem()
}

func (target restoreDestination) writable() bool {
	return target.pointer != nil && !target.reflected.IsValid()
}

// field requires an in-range offset within target's actual allocation.
// Its caller derives offsets from an array's element size and checked index. Zero-sized elements retain the same valid pointer.
func (target restoreDestination) field(plan *restorePlan, offset uintptr) restoreDestination {
	return restoreDestination{plan: plan, pointer: unsafe.Add(target.pointer, offset)}
}

func (target restoreDestination) directArray() bool {
	return target.writable() && target.plan.kind == reflect.Array
}

func (target restoreDestination) length() int {
	if target.directArray() {
		return target.plan.typ.Len()
	}
	// In particular, a nil *[0]T has no actual array view: do not use its
	// type's zero length to turn the old invalid-value error into success.
	return target.value().Len()
}

func (target restoreDestination) storeBool(value bool) {
	if target.writable() && target.plan.kind == reflect.Bool {
		*(*bool)(target.pointer) = value
		return
	}
	target.value().SetBool(value)
}

func (target restoreDestination) storeString(value string) {
	if target.writable() && target.plan.kind == reflect.String {
		// An ordinary typed string assignment retains the write barrier.
		*(*string)(target.pointer) = value
		return
	}
	target.value().SetString(value)
}

func (target restoreDestination) storePointer(pointer reflect.Value) {
	if target.writable() && target.plan.kind == reflect.Pointer && pointer.Type().AssignableTo(target.plan.typ) {
		// This is a pointer assignment, not an integer/header copy. The
		// compiler emits its ordinary GC write barrier for the actual slot.
		*(*unsafe.Pointer)(target.pointer) = pointer.UnsafePointer()
		return
	}
	target.value().Set(pointer)
}

func (target restoreDestination) storeInt(value int64) int64 {
	if target.writable() {
		switch target.plan.kind {
		case reflect.Int:
			stored := int(value)
			*(*int)(target.pointer) = stored
			return int64(stored)
		case reflect.Int8:
			stored := int8(value)
			*(*int8)(target.pointer) = stored
			return int64(stored)
		case reflect.Int16:
			stored := int16(value)
			*(*int16)(target.pointer) = stored
			return int64(stored)
		case reflect.Int32:
			stored := int32(value)
			*(*int32)(target.pointer) = stored
			return int64(stored)
		case reflect.Int64:
			stored := int64(value)
			*(*int64)(target.pointer) = stored
			return int64(stored)
		}
	}
	object := target.value()
	object.SetInt(value)
	return object.Int()
}

func (target restoreDestination) storeUint(value uint64) uint64 {
	if target.writable() {
		switch target.plan.kind {
		case reflect.Uint:
			stored := uint(value)
			*(*uint)(target.pointer) = stored
			return uint64(stored)
		case reflect.Uint8:
			stored := uint8(value)
			*(*uint8)(target.pointer) = stored
			return uint64(stored)
		case reflect.Uint16:
			stored := uint16(value)
			*(*uint16)(target.pointer) = stored
			return uint64(stored)
		case reflect.Uint32:
			stored := uint32(value)
			*(*uint32)(target.pointer) = stored
			return uint64(stored)
		case reflect.Uint64:
			stored := uint64(value)
			*(*uint64)(target.pointer) = stored
			return uint64(stored)
		case reflect.Uintptr:
			stored := uintptr(value)
			*(*uintptr)(target.pointer) = stored
			return uint64(stored)
		}
	}
	object := target.value()
	object.SetUint(value)
	return object.Uint()
}

func (target restoreDestination) storeFloat(value float64) float64 {
	if target.writable() {
		switch target.plan.kind {
		case reflect.Float32:
			stored := float32(value)
			*(*float32)(target.pointer) = stored
			return float64(stored)
		case reflect.Float64:
			stored := float64(value)
			*(*float64)(target.pointer) = stored
			return float64(stored)
		}
	}
	object := target.value()
	object.SetFloat(value)
	return object.Float()
}

func (target restoreDestination) storeComplex(value complex128) complex128 {
	if target.writable() {
		switch target.plan.kind {
		case reflect.Complex64:
			stored := complex64(value)
			*(*complex64)(target.pointer) = stored
			return complex128(stored)
		case reflect.Complex128:
			stored := complex128(value)
			*(*complex128)(target.pointer) = stored
			return complex128(stored)
		}
	}
	object := target.value()
	object.SetComplex(value)
	return object.Complex()
}
