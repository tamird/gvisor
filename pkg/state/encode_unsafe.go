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

// captureArray selects one save-owned array representation without
// reflecting or boxing each primitive or pointer element. Kind selects its
// native width and layout, including defined scalar types. Typed copies retain
// GC ownership for strings. Other kinds and unaddressable values use encodeArray's
// existing path. Each invocation captures anew; late parent replacement must
// remain able to observe changes made before re-encoding a containing array.
func (es *encodeState) captureArray(obj reflect.Value) (wire.Object, bool) {
	if !obj.CanAddr() {
		return nil, false
	}
	var captured wire.Object
	length := obj.Len()
	switch obj.Type().Elem().Kind() {
	case reflect.Ptr:
		es.encodeReferenceArray(obj, &captured)
		return captured, true
	case reflect.Bool:
		captured = wire.CaptureBoolArray(unsafe.Slice((*bool)(obj.Addr().UnsafePointer()), length))
	case reflect.Int:
		captured = wire.CaptureIntArray(unsafe.Slice((*int)(obj.Addr().UnsafePointer()), length))
	case reflect.Int8:
		captured = wire.CaptureIntArray(unsafe.Slice((*int8)(obj.Addr().UnsafePointer()), length))
	case reflect.Int16:
		captured = wire.CaptureIntArray(unsafe.Slice((*int16)(obj.Addr().UnsafePointer()), length))
	case reflect.Int32:
		captured = wire.CaptureIntArray(unsafe.Slice((*int32)(obj.Addr().UnsafePointer()), length))
	case reflect.Int64:
		captured = wire.CaptureIntArray(unsafe.Slice((*int64)(obj.Addr().UnsafePointer()), length))
	case reflect.Uint:
		captured = wire.CaptureUintArray(unsafe.Slice((*uint)(obj.Addr().UnsafePointer()), length))
	case reflect.Uint8:
		captured = wire.CaptureUintArray(unsafe.Slice((*uint8)(obj.Addr().UnsafePointer()), length))
	case reflect.Uint16:
		captured = wire.CaptureUintArray(unsafe.Slice((*uint16)(obj.Addr().UnsafePointer()), length))
	case reflect.Uint32:
		captured = wire.CaptureUintArray(unsafe.Slice((*uint32)(obj.Addr().UnsafePointer()), length))
	case reflect.Uint64:
		captured = wire.CaptureUintArray(unsafe.Slice((*uint64)(obj.Addr().UnsafePointer()), length))
	case reflect.Uintptr:
		captured = wire.CaptureUintArray(unsafe.Slice((*uintptr)(obj.Addr().UnsafePointer()), length))
	case reflect.Float32:
		captured = wire.CaptureFloat32Array(unsafe.Slice((*float32)(obj.Addr().UnsafePointer()), length))
	case reflect.Float64:
		captured = wire.CaptureFloat64Array(unsafe.Slice((*float64)(obj.Addr().UnsafePointer()), length))
	case reflect.Complex64:
		captured = wire.CaptureComplex64Array(unsafe.Slice((*complex64)(obj.Addr().UnsafePointer()), length))
	case reflect.Complex128:
		captured = wire.CaptureComplex128Array(unsafe.Slice((*complex128)(obj.Addr().UnsafePointer()), length))
	case reflect.String:
		captured = wire.CaptureStringArray(unsafe.Slice((*string)(obj.Addr().UnsafePointer()), length))
	default:
		return nil, false
	}
	return captured, true
}

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
