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

// mapDecoder owns the collection algorithm. Typed and dynamic destinations
// supply only Go allocation and assignment, not separate restoration paths.
func mapDecoder(keyType, valueType *valueDecoder, ensure func(any), insert func(any, any, any)) func(*decodeState, *objectDecodeState, any, wire.Object) {
	return func(ds *decodeState, ods *objectDecodeState, pointer any, value wire.Object) {
		if ref, ok := value.(*wire.Ref); ok {
			ds.restoreReference(reflect.ValueOf(pointer).Elem(), ref)
			return
		}
		encoded := requireWire[*wire.Map](value)
		ensure(pointer)
		// Aggregate cells remain distinct because deferred loaders can
		// retain their addresses. Other values are copied on insertion.
		reuseKey := keyType.typ.Kind() != reflect.Struct && keyType.typ.Kind() != reflect.Array
		reuseValue := valueType.typ.Kind() != reflect.Struct && valueType.typ.Kind() != reflect.Array
		var keyCell, valueCell any
		for i, key := range encoded.Keys {
			value := encoded.Values[i]
			if i == 0 || !reuseKey {
				keyCell = keyType.new()
			} else {
				// Nil wire values leave the destination unchanged.
				keyType.zero(keyCell)
			}
			if i == 0 || !reuseValue {
				valueCell = valueType.new()
			} else {
				valueType.zero(valueCell)
			}
			keyType.restore(ds, ods, keyCell, key)
			valueType.restore(ds, ods, valueCell, value)
			ds.waitObject(ods, key, nil)
			ds.waitObject(ods, value, nil)
			// Read the current map header: a loader may have replaced it.
			insert(pointer, keyCell, valueCell)
		}
	}
}

func sliceDecoder(element *valueDecoder, length func(any) int, index func(any, int) any, base func(any) unsafe.Pointer) func(*decodeState, *objectDecodeState, any, wire.Object) {
	return func(ds *decodeState, ods *objectDecodeState, pointer any, value wire.Object) {
		if slice, ok := value.(*wire.Slice); ok {
			ds.assignSlice(reflect.ValueOf(pointer).Elem(), slice)
			return
		}
		// A manual saver may supply an array to an existing slice. Element
		// hooks can replace the slice, so index must read its live header.
		ds.restoreArray(ods, element, length(pointer), func(i int) any { return index(pointer, i) },
			func() unsafe.Pointer { return base(pointer) }, requireWire[wire.Array](value))
	}
}

func (ds *decodeState) restoreArray(ods *objectDecodeState, element *valueDecoder, length int, index func(int) any, base func() unsafe.Pointer, encoded wire.Array) {
	if encoded.Len() != length {
		Failf("mismatching array length expect=%d, actual=%d", length, encoded.Len())
	}
	if length == 0 {
		return
	}
	// General arrays arise from interface and custom savers as well as
	// generated structs. Their elements still use the destination decoder.
	if values, ok := encoded.(*wire.ObjectArray); ok {
		for i, value := range values.Contents {
			element.restore(ds, ods, index(i), value)
			ds.waitObject(ods, value, nil)
		}
		return
	}
	if values, ok := encoded.(*wire.RefArray); ok {
		for i := range values.Contents {
			value := &values.Contents[i]
			element.restore(ds, ods, index(i), value)
			ds.waitObject(ods, value, nil)
		}
		return
	}
	if element.array == nil {
		Failf("array %T cannot restore elements of %s", encoded, element.typ)
	}
	element.array(base(), length, encoded)
}
