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

// mapOperations adapts declared or dynamic maps to the shared decoder. It owns
// neither object identity nor insertion scheduling.
type mapOperations struct {
	assign     func(location, location)
	initialize func(location)
	insert     func(location, location, location)
}

var reflectedMapOperations = &mapOperations{
	assign: func(dest, source location) { dest.reflect().Set(source.reflect()) },
	initialize: func(source location) {
		obj := source.reflect()
		if obj.IsNil() {
			obj.Set(reflect.MakeMap(obj.Type()))
		}
	},
	insert: func(source, key, element location) {
		source.reflect().SetMapIndex(key.reflect(), element.reflect())
	},
}

// RegisterMap stages native decoding operations discovered by stateify. It
// adds no wire name. Discovery is idempotent and must finish during init.
func RegisterMap[M ~map[K]V, K comparable, V any]() {
	operationRegistry.Lock()
	defer operationRegistry.Unlock()
	definition := definitionFor(reflect.TypeFor[M]())
	if definition.mapOps != nil {
		return
	}
	definition.mapOps = &mapOperations{
		assign: func(dest, source location) { *(*M)(dest.address) = *(*M)(source.address) },
		initialize: func(source location) {
			m := (*M)(source.address)
			if *m == nil {
				*m = make(M)
			}
		},
		insert: func(source, key, element location) {
			(*(*M)(source.address))[*(*K)(key.address)] = *(*V)(element.address)
		},
	}
}

type sliceOperations struct {
	set func(location, location, int, int)
}

var reflectedSliceOperations = &sliceOperations{
	set: func(dest, array location, length, capacity int) {
		dest.reflect().Set(reflectValueRWSlice3(array.reflect(), 0, length, capacity))
	},
}

// RegisterSlice stages native slice assignment. The shared decoder validates
// the actual backing element type, length and capacity before calling it.
func RegisterSlice[S ~[]E, E any]() {
	operationRegistry.Lock()
	defer operationRegistry.Unlock()
	definition := definitionFor(reflect.TypeFor[S]())
	if definition.slice != nil {
		return
	}
	definition.slice = &sliceOperations{
		set: func(dest, array location, length, capacity int) {
			*(*S)(dest.address) = S(unsafe.Slice((*E)(array.address), capacity)[:length:capacity])
		},
	}
}
