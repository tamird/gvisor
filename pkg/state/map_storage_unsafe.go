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

// mapStorage only accesses Go storage. The common map encoder and decoder own
// wire representation, retained cells, zeroing, dependencies and graph edges.
type mapStorage struct {
	newKey   func(reflect.Value) reflect.Value
	newValue func(reflect.Value) reflect.Value
	insert   func(reflect.Value, reflect.Value, reflect.Value)
	visit    func(reflect.Value, reflect.Value, reflect.Value, func(reflect.Value, reflect.Value))
}

// mapStorageProvider supplies an immutable storage adapter. Selection happens
// once inside each map traversal; no adapter is retained by the object graph.
type mapStorageProvider interface {
	stateMapStorage() *mapStorage
}

func storageForMap(object reflect.Value) *mapStorage {
	if object.CanSet() && object.Type().Name() != "" {
		if provider, ok := object.Addr().Interface().(mapStorageProvider); ok {
			return provider.stateMapStorage()
		}
	}
	return &reflectedMap
}

var reflectedMap = mapStorage{
	newKey: func(v reflect.Value) reflect.Value {
		return reflect.New(v.Type().Key()).Elem()
	},
	newValue: func(v reflect.Value) reflect.Value {
		return reflect.New(v.Type().Elem()).Elem()
	},
	insert: func(v, key, value reflect.Value) {
		v.SetMapIndex(key, value)
	},
	visit: func(v, key, value reflect.Value, visit func(reflect.Value, reflect.Value)) {
		iter := v.MapRange()
		for iter.Next() {
			key.SetIterKey(iter)
			value.SetIterValue(iter)
			visit(key, value)
		}
	},
}

// typedStorage checks the exact Go type and write permissions before exposing
// an address. The immediate UnsafeAddr conversion keeps the retained value's
// storage live; typed stores retain Go's write barriers for pointer fields.
func typedStorage[T any](v reflect.Value) *T {
	if !v.CanSet() || v.Type() != reflect.TypeFor[T]() {
		panic("incompatible typed storage")
	}
	return (*T)(unsafe.Pointer(v.UnsafeAddr()))
}

func typedMapStorage[M ~map[K]V, K comparable, V any]() mapStorage {
	return mapStorage{
		newKey:   func(reflect.Value) reflect.Value { return reflect.ValueOf(new(K)).Elem() },
		newValue: func(reflect.Value) reflect.Value { return reflect.ValueOf(new(V)).Elem() },
		insert: func(v, k, x reflect.Value) {
			(*typedStorage[M](v))[*typedStorage[K](k)] = *typedStorage[V](x)
		},
		visit: func(v, k, x reflect.Value, visit func(reflect.Value, reflect.Value)) {
			object, keySlot, valueSlot := typedStorage[M](v), typedStorage[K](k), typedStorage[V](x)
			for key, value := range *object {
				*keySlot, *valueSlot = key, value
				visit(k, x)
			}
		},
	}
}
