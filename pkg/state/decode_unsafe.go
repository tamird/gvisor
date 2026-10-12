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

// walkChild returns a child object from obj, given an accessor path. This is
// the decode-side equivalent to traverse in encode.go.
//
// For the purposes of this function, a child object is either a field within a
// struct or an array element, with one such indirection per element in
// path. The returned value may be an unexported field, so it may not be
// directly assignable. See decode_unsafe.go.
func walkChild(path []wire.Dot, obj reflect.Value) reflect.Value {
	if len(path) == 0 {
		return obj
	}
	layout := layoutFor(obj.Type())
	base := obj.Addr().UnsafePointer()
	offset := uintptr(0)
	for i := len(path) - 1; i >= 0; i-- {
		switch component := path[i].(type) {
		case *wire.FieldName:
			if layout.kind != reflect.Struct {
				Failf("field path in non-struct type %v", layout.typ)
			}
			var field *layoutField
			for j := range layout.fields {
				if layout.fields[j].name == string(*component) {
					field = &layout.fields[j]
					break
				}
			}
			if field == nil {
				// Current encode paths name direct physical fields. Retain
				// FieldByName's established acceptance of promoted fields,
				// including its checked embedded-pointer traversal.
				child := reflect.NewAt(layout.typ, unsafe.Add(base, offset)).Elem().FieldByName(string(*component))
				if !child.IsValid() {
					Failf("unknown field %q in type %v", *component, layout.typ)
				}
				base, offset = child.Addr().UnsafePointer(), 0
				layout = layoutFor(child.Type())
				continue
			}
			offset += field.offset
			layout = field.layout
		case wire.Index:
			if layout.kind != reflect.Array || uint64(component) >= uint64(layout.length) {
				Failf("invalid array index %d in type %v", component, layout.typ)
			}
			offset += uintptr(component) * layout.elem.size
			layout = layout.elem
		default:
			Failf("invalid child path component %T", component)
		}
	}
	// Native offsets are bounded by the current Go layout. A promoted-field
	// traversal establishes a new checked base through public reflection.
	return reflect.NewAt(layout.typ, unsafe.Add(base, offset)).Elem()
}

func (ds *decodeState) decodeValue(ods *objectDecodeState, obj value, encoded wire.Object) {
	switch x := encoded.(type) {
	case wire.Nil:
		// Preserve the existing no-op, including an interface's imbued type.
	case *wire.Ref:
		if x.Root == 0 {
			return
		}
		if obj.ops.layout.kind == reflect.Map {
			target := reflectedValue(ds.register(x, obj.ops.layout.typ))
			if target.ops.layout.kind != reflect.Map || (target.ops.layout.typ != obj.ops.layout.typ && !target.ops.layout.typ.AssignableTo(obj.ops.layout.typ)) {
				Failf("map reference type %v does not match %v", target.ops.layout.typ, obj.ops.layout.typ)
			}
			target.ops.mapOps.initialize(target)
			if target.ops.layout.typ == obj.ops.layout.typ {
				obj.ops.mapOps.assign(obj, target)
			} else {
				// Compatible named/unnamed map types use Go's public
				// assignment boundary, not an unchecked native reinterpretation.
				obj.reflect().Set(target.reflect())
			}
			return
		}
		obj.require(reflect.Ptr)
		target := ds.register(x, obj.ops.elem.layout.typ)
		if target.Type() != obj.ops.elem.layout.typ {
			Failf("pointer target type %v does not match %v", target.Type(), obj.ops.elem.layout.typ)
		}
		*(*unsafe.Pointer)(obj.address) = target.Addr().UnsafePointer()
	case wire.Bool:
		obj.require(reflect.Bool)
		*(*bool)(obj.address) = bool(x)
	case wire.Int:
		obj.setSigned(int64(x))
	case wire.Uint:
		obj.setUnsigned(uint64(x))
	case wire.Float32:
		obj.setFloat(float64(x), false)
	case wire.Float64:
		obj.setFloat(float64(x), true)
	case *wire.Complex64:
		obj.setComplex(complex128(*x), false)
	case *wire.Complex128:
		obj.setComplex(complex128(*x), true)
	case *wire.String:
		obj.require(reflect.String)
		*(*string)(obj.address) = string(*x)
	case *wire.Slice:
		if x.Ref.Root == 0 {
			return
		}
		obj.require(reflect.Slice)
		capacity := checkedArrayLength(uint64(x.Capacity), obj.ops.elem.layout.size)
		if x.Length > x.Capacity {
			Failf("slice length %d exceeds capacity %d", x.Length, x.Capacity)
		}
		typ := reflect.ArrayOf(capacity, obj.ops.elem.layout.typ)
		target := reflectedValue(ds.register(&x.Ref, typ))
		if target.ops.layout.kind != reflect.Array || target.ops.elem.layout.typ != obj.ops.elem.layout.typ || target.ops.layout.length < uintptr(capacity) {
			Failf("slice backing type %v does not contain %d elements of %v", target.ops.layout.typ, capacity, obj.ops.elem.layout.typ)
		}
		obj.ops.slice.set(obj, target, int(x.Length), capacity)
	case *wire.Array:
		obj.require(reflect.Array)
		ds.decodeArray(ods, obj, x)
	case *wire.Struct:
		obj.require(reflect.Struct)
		ds.decodeStruct(ods, obj.reflect(), x)
	case *wire.Map:
		obj.require(reflect.Map)
		if len(x.Keys) != len(x.Values) {
			Failf("map has %d keys and %d values", len(x.Keys), len(x.Values))
		}
		ds.decodeMap(ods, obj, x)
	case *wire.Interface:
		obj.require(reflect.Interface)
		ds.decodeInterface(ods, obj.reflect(), x)
	default:
		Failf("unknown object %#v for %q", encoded, obj.ops.layout.typ.Name())
	}
}

func checkedArrayLength(count uint64, elementSize uintptr) int {
	maxInt := uint64(^uint(0) >> 1)
	if count > maxInt || (elementSize != 0 && count > maxInt/uint64(elementSize)) {
		Failf("array length %d overflows storage for %d-byte elements", count, elementSize)
	}
	return int(count)
}
