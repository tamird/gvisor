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

	"gvisor.dev/gvisor/pkg/state/wire"
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

// storeScalar adapts a runtime-known destination to the same native-width
// stores used by checked generated fields. It retains no reflected value or
// address.
//
// Preconditions: obj is addressable Go storage.
func storeScalar(obj reflect.Value, encoded wire.Object) bool {
	switch obj.Kind() {
	case reflect.Bool:
		return storeBool((*bool)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Int:
		return storeSigned((*int)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Int8:
		return storeSigned((*int8)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Int16:
		return storeSigned((*int16)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Int32:
		return storeSigned((*int32)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Int64:
		return storeSigned((*int64)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Uint:
		return storeUnsigned((*uint)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Uint8:
		return storeUnsigned((*uint8)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Uint16:
		return storeUnsigned((*uint16)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Uint32:
		return storeUnsigned((*uint32)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Uint64:
		return storeUnsigned((*uint64)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Uintptr:
		return storeUnsigned((*uintptr)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Float32:
		return storeFloat((*float32)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Float64:
		return storeFloat((*float64)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Complex64:
		return storeComplex((*complex64)(obj.Addr().UnsafePointer()), encoded)
	case reflect.Complex128:
		return storeComplex((*complex128)(obj.Addr().UnsafePointer()), encoded)
	case reflect.String:
		return storeString((*string)(obj.Addr().UnsafePointer()), encoded)
	default:
		return false
	}
}

// assignPointer shares resolution and exact target validation between generated
// pointer fields and the reflected adapter. The native pointer store retains
// the Go write barrier; no uintptr is used to hold a live object address.
//
// Preconditions:
//   - destination points to writable Go storage of exactly typ, a pointer type.
//   - ref.Root is nonzero.
func (ds *decodeState) assignPointer(destination unsafe.Pointer, typ reflect.Type, ref *wire.Ref) {
	target := ds.register(ref, typ.Elem())
	if target.Type() != typ.Elem() {
		Failf("pointer target type %v does not match %v", target.Type(), typ.Elem())
	}
	*(*unsafe.Pointer)(destination) = target.Addr().UnsafePointer()
}
