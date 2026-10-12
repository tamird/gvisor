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
// path. expected may be reused only when its exact type matches the child.
func walkChild(path []wire.Dot, obj location, expected *typeOperations) location {
	if len(path) == 0 {
		return obj
	}
	layout := obj.ops.layout
	base := obj.address
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
	ops := expected
	if ops == nil || ops.layout.typ != layout.typ {
		ops = operationsFor(layout.typ)
	}
	return location{ops: ops, address: unsafe.Add(base, offset)}
}

// decoderForKind selects one shared operation per built-in destination kind.
// It does not create per-user-type closures or reject unsupported kinds before
// decodeObject has handled the universal nil no-op.
func decoderForKind(kind reflect.Kind) func(*decodeState, *objectDecodeState, location, wire.Object) {
	switch kind {
	case reflect.Bool:
		return decodeBool
	case reflect.Int:
		return decodeSigned[int]
	case reflect.Int8:
		return decodeSigned[int8]
	case reflect.Int16:
		return decodeSigned[int16]
	case reflect.Int32:
		return decodeSigned[int32]
	case reflect.Int64:
		return decodeSigned[int64]
	case reflect.Uint:
		return decodeUnsigned[uint]
	case reflect.Uint8:
		return decodeUnsigned[uint8]
	case reflect.Uint16:
		return decodeUnsigned[uint16]
	case reflect.Uint32:
		return decodeUnsigned[uint32]
	case reflect.Uint64:
		return decodeUnsigned[uint64]
	case reflect.Uintptr:
		return decodeUnsigned[uintptr]
	case reflect.Float32:
		return decodeFloat[float32]
	case reflect.Float64:
		return decodeFloat[float64]
	case reflect.Complex64:
		return decodeComplex[complex64]
	case reflect.Complex128:
		return decodeComplex[complex128]
	case reflect.String:
		return decodeString
	case reflect.Ptr:
		return decodePointer
	case reflect.Map:
		return decodeMapLocation
	case reflect.Slice:
		return decodeSlice
	case reflect.Array:
		return (*decodeState).decodeArray
	case reflect.Struct:
		return (*decodeState).decodeStruct
	case reflect.Interface:
		return (*decodeState).decodeInterface
	default:
		return decodeUnsupported
	}
}

func decodeUnsupported(_ *decodeState, _ *objectDecodeState, obj location, _ wire.Object) {
	obj.incompatible()
}

func decodeBool(_ *decodeState, _ *objectDecodeState, obj location, encoded wire.Object) {
	if !storeBool((*bool)(obj.address), encoded) {
		obj.incompatible()
	}
}

func decodeSigned[T signed](_ *decodeState, _ *objectDecodeState, obj location, encoded wire.Object) {
	if !storeSigned((*T)(obj.address), encoded) {
		obj.incompatible()
	}
}

func decodeUnsigned[T unsigned](_ *decodeState, _ *objectDecodeState, obj location, encoded wire.Object) {
	if !storeUnsigned((*T)(obj.address), encoded) {
		obj.incompatible()
	}
}

func decodeFloat[T ~float32 | ~float64](_ *decodeState, _ *objectDecodeState, obj location, encoded wire.Object) {
	if !storeFloat((*T)(obj.address), encoded) {
		obj.incompatible()
	}
}

func decodeComplex[T ~complex64 | ~complex128](_ *decodeState, _ *objectDecodeState, obj location, encoded wire.Object) {
	if !storeComplex((*T)(obj.address), encoded) {
		obj.incompatible()
	}
}

func decodeString(_ *decodeState, _ *objectDecodeState, obj location, encoded wire.Object) {
	if !storeString((*string)(obj.address), encoded) {
		obj.incompatible()
	}
}

// pointerTarget is shared by generated pointer fields and dynamic destinations.
func (ds *decodeState) pointerTarget(encoded wire.Object, typ reflect.Type, expected *typeOperations) location {
	ref, ok := encoded.(*wire.Ref)
	if !ok {
		Failf("wire value has incompatible pointer target type %v", typ)
	}
	target := ds.register(ref, typ, expected)
	if target.ops.layout.typ != typ {
		Failf("pointer target type %v does not match %v", target.ops.layout.typ, typ)
	}
	return target
}

func decodePointer(ds *decodeState, _ *objectDecodeState, obj location, encoded wire.Object) {
	target := ds.pointerTarget(encoded, obj.ops.elem.layout.typ, obj.ops.elem)
	*(*unsafe.Pointer)(obj.address) = target.address
}

func (ds *decodeState) mapTarget(ref *wire.Ref, typ reflect.Type, expected *typeOperations) location {
	target := ds.register(ref, typ, expected)
	if target.ops.layout.kind != reflect.Map || (target.ops.layout.typ != typ && !target.ops.layout.typ.AssignableTo(typ)) {
		Failf("map reference type %v does not match %v", target.ops.layout.typ, typ)
	}
	return target
}

// assignMap keeps Go's assignment boundary for dynamic and assignable named
// map types. References need a non-nil header before the payload is decoded.
func assignMap(dst, target location) {
	if native := operationRegistry.maps[target.ops.layout.typ]; native != nil {
		native.ensure(target.address)
		if dst.ops.layout.typ == target.ops.layout.typ {
			native.assign(dst.address, target.address)
			return
		}
	} else {
		value := target.reflect()
		if value.IsNil() {
			value.Set(reflect.MakeMap(value.Type()))
		}
	}
	// Different assignable map types still use Go's checked assignment.
	dst.reflect().Set(target.reflect())
}

func decodeMapLocation(ds *decodeState, ods *objectDecodeState, obj location, encoded wire.Object) {
	switch x := encoded.(type) {
	case *wire.Ref:
		target := ds.mapTarget(x, obj.ops.layout.typ, obj.ops)
		assignMap(obj, target)
	case *wire.Map:
		if len(x.Keys) != len(x.Values) {
			Failf("map has %d keys and %d values", len(x.Keys), len(x.Values))
		}
		ds.decodeMap(ods, obj, x)
	default:
		obj.incompatible()
	}
}

func (ds *decodeState) sliceTarget(encoded wire.Object, element reflect.Type) (location, int, int) {
	x, ok := encoded.(*wire.Slice)
	if !ok {
		Failf("wire value has incompatible slice element type %v", element)
	}
	capacity := checkedArrayLength(uint64(x.Capacity), element.Size())
	if x.Length > x.Capacity {
		Failf("slice length %d exceeds capacity %d", x.Length, x.Capacity)
	}
	typ := reflect.ArrayOf(capacity, element)
	// An existing backing array may contain a longer prefix than this slice.
	// Resolve its actual operations instead of building an unused prefix type.
	target := ds.register(&x.Ref, typ, nil)
	if target.ops.layout.kind != reflect.Array || target.ops.elem.layout.typ != element || target.ops.layout.length < uintptr(capacity) {
		Failf("slice backing type %v does not contain %d elements of %v", target.ops.layout.typ, capacity, element)
	}
	return target, int(x.Length), capacity
}

func decodeSlice(ds *decodeState, _ *objectDecodeState, obj location, encoded wire.Object) {
	target, length, capacity := ds.sliceTarget(encoded, obj.ops.elem.layout.typ)
	obj.reflect().Set(reflectValueRWSlice3(target.reflect(), 0, length, capacity))
}

func checkedArrayLength(count uint64, elementSize uintptr) int {
	maxInt := uint64(^uint(0) >> 1)
	if count > maxInt || (elementSize != 0 && count > maxInt/uint64(elementSize)) {
		Failf("array length %d overflows storage for %d-byte elements", count, elementSize)
	}
	return int(count)
}
