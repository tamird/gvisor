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

// captureArray selects one save-owned array representation without
// reflecting or boxing each primitive or pointer element. Kind selects its
// native width and layout, including defined scalar types. Typed copies retain
// GC ownership for strings. Other kinds and unaddressable values use encodeArray's
// existing path. Each invocation captures anew; late parent replacement must
// remain able to observe changes made before re-encoding a containing array.
func (es *encodeState) captureArray(obj value) (wire.Object, bool) {
	if obj.address == nil {
		return nil, false
	}
	var captured wire.Object
	length := int(obj.ops.layout.length)
	switch obj.ops.elem.layout.kind {
	case reflect.Ptr:
		es.encodeReferenceArray(obj, &captured)
		return captured, true
	case reflect.Bool:
		captured = wire.CaptureBoolArray(unsafe.Slice((*bool)(obj.address), length))
	case reflect.Int:
		captured = wire.CaptureIntArray(unsafe.Slice((*int)(obj.address), length))
	case reflect.Int8:
		captured = wire.CaptureIntArray(unsafe.Slice((*int8)(obj.address), length))
	case reflect.Int16:
		captured = wire.CaptureIntArray(unsafe.Slice((*int16)(obj.address), length))
	case reflect.Int32:
		captured = wire.CaptureIntArray(unsafe.Slice((*int32)(obj.address), length))
	case reflect.Int64:
		captured = wire.CaptureIntArray(unsafe.Slice((*int64)(obj.address), length))
	case reflect.Uint:
		captured = wire.CaptureUintArray(unsafe.Slice((*uint)(obj.address), length))
	case reflect.Uint8:
		captured = wire.CaptureUintArray(unsafe.Slice((*uint8)(obj.address), length))
	case reflect.Uint16:
		captured = wire.CaptureUintArray(unsafe.Slice((*uint16)(obj.address), length))
	case reflect.Uint32:
		captured = wire.CaptureUintArray(unsafe.Slice((*uint32)(obj.address), length))
	case reflect.Uint64:
		captured = wire.CaptureUintArray(unsafe.Slice((*uint64)(obj.address), length))
	case reflect.Uintptr:
		captured = wire.CaptureUintArray(unsafe.Slice((*uintptr)(obj.address), length))
	case reflect.Float32:
		captured = wire.CaptureFloat32Array(unsafe.Slice((*float32)(obj.address), length))
	case reflect.Float64:
		captured = wire.CaptureFloat64Array(unsafe.Slice((*float64)(obj.address), length))
	case reflect.Complex64:
		captured = wire.CaptureComplex64Array(unsafe.Slice((*complex64)(obj.address), length))
	case reflect.Complex128:
		captured = wire.CaptureComplex128Array(unsafe.Slice((*complex128)(obj.address), length))
	case reflect.String:
		captured = wire.CaptureStringArray(unsafe.Slice((*string)(obj.address), length))
	default:
		return nil, false
	}
	return captured, true
}

// encodeReferenceArray uses the same resolver directly from GC-visible element
// pointers. No reflected scratch pointer or alternate array representation is
// retained.
func (es *encodeState) encodeReferenceArray(obj value, dest *wire.Object) {
	a := &wire.RefArray{Contents: make([]wire.Ref, int(obj.ops.layout.length))}
	*dest = a
	pointers := unsafe.Slice((*unsafe.Pointer)(obj.address), int(obj.ops.layout.length))
	for i, pointer := range pointers {
		if pointer != nil {
			es.resolveValue(value{ops: obj.ops.elem.elem, address: pointer}, uintptr(pointer), &a.Contents[i], encodeDefault)
		}
	}
}

// objectValue owns an addressable graph source. Original reflected snapshot
// keys belong to the inline value adapters, not every retained graph node.
type objectValue struct {
	ops     *typeOperations
	address unsafe.Pointer
}

func (obj objectValue) value() value           { return value{ops: obj.ops, address: obj.address} }
func (obj objectValue) reflect() reflect.Value { return obj.value().reflect() }

// CapturePointer records a pointer in a stable, inline reference slot. The
// resolver may change that slot if a containing object is discovered later.
// The slot must initially be zero and must outlive graph discovery.
func CapturePointer[T any](s Sink, value *T, dest *wire.Ref) {
	if value != nil {
		s.internal.es.resolveValue(valueAt(unsafe.Pointer(value), operationsFor(reflect.TypeFor[T]())), uintptr(unsafe.Pointer(value)), dest, encodeDefault)
	}
}

// CaptureSlice snapshots the slice header, clears its unused capacity, and
// resolves the backing array for deferred capture.
func CaptureSlice[S ~[]E, E any](s Sink, slice S, dest *wire.Slice) {
	source := valueAt(unsafe.Pointer(&slice), operationsFor(reflect.TypeFor[S]()))
	source.ops.slice.capture(s.internal.es, source, dest)
}
