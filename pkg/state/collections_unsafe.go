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

// mapOperations adapts native or dynamically constructed map types to the same
// graph walk. It does not own an encoder, decoder, record, or wire schema.
type mapOperations struct {
	length     func(value) (int, bool)
	assign     func(value, value)
	visit      func(value, func(value, value))
	initialize func(value)
	insert     func(value, value, value)
}

var reflectedMapOperations = &mapOperations{
	length: func(source value) (int, bool) { obj := source.reflect(); return obj.Len(), obj.IsNil() },
	assign: func(dest, source value) { dest.reflect().Set(source.reflect()) },
	visit: func(source value, visit func(value, value)) {
		iter := source.reflect().MapRange()
		for iter.Next() {
			// Copy both entries before either hook can mutate the source map.
			key, element := reflectedValue(iter.Key()), reflectedValue(iter.Value())
			visit(key, element)
		}
	},
	initialize: func(source value) {
		obj := source.reflect()
		if obj.IsNil() {
			obj.Set(reflect.MakeMap(obj.Type()))
		}
	},
	insert: func(source, key, element value) {
		source.reflect().SetMapIndex(key.reflect(), element.reflect())
	},
}

// RegisterMap stages native map operations discovered by stateify. It adds no
// wire name: named types still use their existing Type registration. Repeated
// discovery is idempotent, and registration must finish during init.
func RegisterMap[M ~map[K]V, K comparable, V any]() {
	operationRegistry.Lock()
	defer operationRegistry.Unlock()
	typ := reflect.TypeFor[M]()
	if definition := reverseTypeDatabase[typ]; definition != nil && definition.mapOps != nil {
		return
	}
	addMemory(typ, memoryFor[M])
	addMemory(reflect.TypeFor[K](), memoryFor[K])
	addMemory(reflect.TypeFor[V](), memoryFor[V])
	read := func(source value) M {
		if source.address != nil {
			return *(*M)(source.address)
		}
		return source.reflected.Interface().(M)
	}
	reverseTypeDatabase[typ].mapOps = &mapOperations{
		length: func(source value) (int, bool) { m := read(source); return len(m), m == nil },
		assign: func(dest, source value) { *(*M)(dest.address) = *(*M)(source.address) },
		visit: func(source value, visit func(value, value)) {
			// Interface reads the live slot at deferred-capture time. A value
			// capture instead owns its copied map header from the outset.
			for key, element := range read(source) {
				k := mapEntry(&key, source.ops.key)
				v := mapEntry(&element, source.ops.elem)
				visit(k, v)
			}
		},
		initialize: func(source value) {
			m := (*M)(source.address)
			if *m == nil {
				*m = make(M)
			}
		},
		insert: func(source, key, element value) {
			(*(*M)(source.address))[*(*K)(key.address)] = *(*V)(element.address)
		},
	}
}

// mapEntry preserves the unaddressable snapshot identity of MapRange while
// retaining the separate stable native receiver. Interface on an addressable
// cell copies indirect values; ValueOf(T) alone can reuse boxed equal values.
// Scalars and references have no struct snapshot key and need no such copy.
func mapEntry[T any](cell *T, ops *typeOperations) value {
	source := valueAt(unsafe.Pointer(cell), ops)
	if ops.layout.kind == reflect.Struct || ops.layout.kind == reflect.Array {
		source.reflected = reflect.ValueOf(reflect.ValueOf(cell).Elem().Interface())
	}
	return source
}

type arrayAllocator func(int) unsafe.Pointer

type sliceOperations struct {
	capture func(*encodeState, value, *wire.Slice)
	// array allocates a GC-scanned backing array of a runtime extent.
	array arrayAllocator
	set   func(value, value, int, int)
}

var reflectedSliceOperations = &sliceOperations{
	set: func(dest, array value, length, capacity int) {
		dest.reflect().Set(reflectValueRWSlice3(array.reflect(), 0, length, capacity))
	},
	capture: func(es *encodeState, source value, dest *wire.Slice) {
		obj := source.reflect()
		dest.Length, dest.Capacity = wire.Uint(obj.Len()), wire.Uint(obj.Cap())
		if obj.IsNil() {
			return
		}
		typ := reflect.ArrayOf(obj.Cap(), source.ops.elem.layout.typ)
		array := reflect.NewAt(typ, obj.UnsafePointer()).Elem()
		if obj.Len() < obj.Cap() {
			array.Slice(obj.Len(), obj.Cap()).Clear()
		}
		es.resolveValue(reflectedValue(array), uintptr(obj.UnsafePointer()), &dest.Ref, encodeDefault)
	},
}

// RegisterSlice stages native header capture, capacity clearing and backing
// allocation. Runtime capacity remains part of the exact array descriptor.
func RegisterSlice[S ~[]E, E any]() {
	operationRegistry.Lock()
	defer operationRegistry.Unlock()
	typ := reflect.TypeFor[S]()
	if definition := reverseTypeDatabase[typ]; definition != nil && definition.slice != nil {
		return
	}
	addMemory(typ, memoryFor[S])
	addMemory(reflect.TypeFor[E](), memoryFor[E])
	reverseTypeDatabase[typ].slice = &sliceOperations{
		capture: func(es *encodeState, source value, dest *wire.Slice) {
			var s S
			if source.address != nil {
				s = *(*S)(source.address)
			} else {
				s = source.reflected.Interface().(S)
			}
			dest.Length, dest.Capacity = wire.Uint(len(s)), wire.Uint(cap(s))
			if s == nil {
				return
			}
			clear(s[len(s):cap(s)])
			ops := operationsFor(reflect.ArrayOf(cap(s), reflect.TypeFor[E]()))
			p := unsafe.Pointer(unsafe.SliceData(s))
			es.resolveValue(value{ops: ops, address: p}, uintptr(p), &dest.Ref, encodeDefault)
		},
		array: func(capacity int) unsafe.Pointer { return unsafe.Pointer(unsafe.SliceData(make([]E, capacity))) },
		set: func(dest, array value, length, capacity int) {
			*(*S)(dest.address) = S(unsafe.Slice((*E)(array.address), capacity)[:length:capacity])
		},
	}
	if reverseTypeDatabase[reflect.TypeFor[E]()].array == nil {
		reverseTypeDatabase[reflect.TypeFor[E]()].array = reverseTypeDatabase[typ].slice.array
	}
}
