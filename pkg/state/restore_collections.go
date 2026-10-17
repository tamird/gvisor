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

	"gvisor.dev/gvisor/pkg/state/wire"
)

// restoreArray owns length checks and the homogeneous wire-kind dispatch.
// Composite elements can invoke hooks, so their destination is indexed anew
// for each element. Primitive elements have no graph dependencies or hooks.
func (ds *decodeState) restoreArray(ods *objectDecodeState, target reflect.Value, encoded wire.Array) {
	length := target.Len()
	if encoded.Len() != length {
		Failf("mismatching array length expect=%d, actual=%d", length, encoded.Len())
	}
	if length == 0 {
		// Preserve legacy empty Array operations on other Len-capable types.
		return
	}
	switch values := encoded.(type) {
	case *wire.ObjectArray:
		for i, value := range values.Contents {
			ds.restoreValue(ods, target.Index(i), value)
			ds.waitObject(ods, value, nil)
		}
		return
	case *wire.RefArray:
		for i := range values.Contents {
			value := &values.Contents[i]
			ds.restoreReference(target.Index(i), value)
			ds.waitObject(ods, value, nil)
		}
		return
	}

	// The actual reflected flags must allow writes, even for an unexported
	// field. A slice value itself need not be addressable; its elements must
	// be. Defined scalar types have the same memory layout as their Kind.
	element := target.Index(0)
	if !element.CanSet() {
		Failf("array element of type %s is not settable", element.Type())
	}
	pointer := element.Addr().UnsafePointer()
	kind := element.Kind()
	switch values := encoded.(type) {
	case *wire.BoolArray:
		if kind == reflect.Bool {
			copy(unsafe.Slice((*bool)(pointer), length), values.Contents)
			return
		}
	case *wire.IntArray[wire.Int]:
		switch kind {
		case reflect.Int:
			restoreIntArray(unsafe.Slice((*int)(pointer), length), values.Contents)
		case reflect.Int8:
			restoreIntArray(unsafe.Slice((*int8)(pointer), length), values.Contents)
		case reflect.Int16:
			restoreIntArray(unsafe.Slice((*int16)(pointer), length), values.Contents)
		case reflect.Int32:
			restoreIntArray(unsafe.Slice((*int32)(pointer), length), values.Contents)
		case reflect.Int64:
			restoreIntArray(unsafe.Slice((*int64)(pointer), length), values.Contents)
		default:
			Failf("signed array cannot restore %s", element.Type())
		}
		return
	case *wire.UintArray[wire.Uint]:
		switch kind {
		case reflect.Uint:
			restoreUintArray(unsafe.Slice((*uint)(pointer), length), values.Contents)
		case reflect.Uint8:
			restoreUintArray(unsafe.Slice((*uint8)(pointer), length), values.Contents)
		case reflect.Uint16:
			restoreUintArray(unsafe.Slice((*uint16)(pointer), length), values.Contents)
		case reflect.Uint32:
			restoreUintArray(unsafe.Slice((*uint32)(pointer), length), values.Contents)
		case reflect.Uint64:
			restoreUintArray(unsafe.Slice((*uint64)(pointer), length), values.Contents)
		case reflect.Uintptr:
			restoreUintArray(unsafe.Slice((*uintptr)(pointer), length), values.Contents)
		default:
			Failf("unsigned array cannot restore %s", element.Type())
		}
		return
	case *wire.Float32Array[wire.Float32]:
		switch kind {
		case reflect.Float32:
			restoreFloatArray(unsafe.Slice((*float32)(pointer), length), values.Contents, false)
		case reflect.Float64:
			restoreFloatArray(unsafe.Slice((*float64)(pointer), length), values.Contents, false)
		default:
			Failf("floating point array cannot restore %s", element.Type())
		}
		return
	case *wire.Float64Array:
		switch kind {
		case reflect.Float32:
			restoreFloatArray(unsafe.Slice((*float32)(pointer), length), values.Contents, true)
		case reflect.Float64:
			restoreFloatArray(unsafe.Slice((*float64)(pointer), length), values.Contents, true)
		default:
			Failf("floating point array cannot restore %s", element.Type())
		}
		return
	case *wire.Complex64Array:
		switch kind {
		case reflect.Complex64:
			restoreComplexArray(unsafe.Slice((*complex64)(pointer), length), values.Contents, false)
		case reflect.Complex128:
			restoreComplexArray(unsafe.Slice((*complex128)(pointer), length), values.Contents, false)
		default:
			Failf("complex array cannot restore %s", element.Type())
		}
		return
	case *wire.Complex128Array:
		switch kind {
		case reflect.Complex64:
			restoreComplexArray(unsafe.Slice((*complex64)(pointer), length), values.Contents, true)
		case reflect.Complex128:
			restoreComplexArray(unsafe.Slice((*complex128)(pointer), length), values.Contents, true)
		default:
			Failf("complex array cannot restore %s", element.Type())
		}
		return
	case *wire.StringArray:
		if kind == reflect.String {
			// Typed string assignment retains Go's GC write barriers. No raw
			// byte copy or pointer-sized store is used for pointerful data.
			copy(unsafe.Slice((*string)(pointer), length), values.Contents)
			return
		}
	}
	Failf("array %T cannot restore %s", encoded, target.Type())
}

// Narrowing stores precede validation, matching reflect.SetInt/SetUint and
// the existing scalar decoder's partial-write behavior on a malformed value.
func restoreIntArray[T ~int | ~int8 | ~int16 | ~int32 | ~int64](target []T, values []wire.Int) {
	for i, value := range values {
		target[i] = T(value)
		checkInt(int64(value), int64(target[i]))
	}
}

func restoreUintArray[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr](target []T, values []wire.Uint) {
	for i, value := range values {
		target[i] = T(value)
		checkUint(uint64(value), uint64(target[i]))
	}
}

func restoreFloatArray[T, S ~float32 | ~float64](target []T, values []S, check bool) {
	for i, value := range values {
		// Float32 payloads passed through float64 before SetFloat, including
		// its signaling-NaN conversion. Preserve that promotion here.
		promoted := float64(value)
		target[i] = T(promoted)
		if check {
			checkFloat(promoted, float64(target[i]))
		}
	}
}

func restoreComplexArray[T ~complex64 | ~complex128, S ~complex128](target []T, values []S, check bool) {
	for i, value := range values {
		target[i] = T(value)
		if check {
			checkComplex(complex128(value), complex128(target[i]))
		}
	}
}

// restoreMap owns temporary cells and Go's map insertion semantics. Aggregate
// cells remain distinct because deferred loaders can retain their addresses.
// Waits precede insertion, which reads the live map header.
func (ds *decodeState) restoreMap(ods *objectDecodeState, object reflect.Value, encoded *wire.Map) {
	if object.IsNil() {
		object.Set(reflect.MakeMap(object.Type()))
	}
	if len(encoded.Keys) == 0 {
		return
	}
	keyType, valueType := object.Type().Key(), object.Type().Elem()
	// Other kinds are copied into the map; interface values get independent
	// dynamic storage in decodeInterface.
	reuseKey := keyType.Kind() != reflect.Struct && keyType.Kind() != reflect.Array
	reuseValue := valueType.Kind() != reflect.Struct && valueType.Kind() != reflect.Array
	var keyCell, valueCell reflect.Value
	for i, key := range encoded.Keys {
		value := encoded.Values[i]
		if i == 0 || !reuseKey {
			keyCell = reflect.New(keyType).Elem()
		} else {
			// Nil wire values leave the destination unchanged.
			keyCell.SetZero()
		}
		if i == 0 || !reuseValue {
			valueCell = reflect.New(valueType).Elem()
		} else {
			valueCell.SetZero()
		}
		ds.restoreValue(ods, keyCell, key)
		ds.restoreValue(ods, valueCell, value)
		ds.waitObject(ods, key, nil)
		ds.waitObject(ods, value, nil)
		object.SetMapIndex(keyCell, valueCell)
	}
}
