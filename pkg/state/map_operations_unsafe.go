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

// mapOperations supplies storage operations for one exact map type. Decoding,
// graph identity, dependencies and insertion order belong to the common loop.
// Addresses must refer to writable Go storage of the corresponding exact type.
type mapOperations interface {
	newMap() unsafe.Pointer
	ensure(unsafe.Pointer)
	cells() (unsafe.Pointer, unsafe.Pointer)
	insert(unsafe.Pointer, unsafe.Pointer, unsafe.Pointer)
	assign(unsafe.Pointer, unsafe.Pointer)
}

type typedMapOperations[M ~map[K]V, K comparable, V any] struct{}

func (typedMapOperations[M, K, V]) newMap() unsafe.Pointer {
	// A pointer or interface may refer to a nil map. Allocate its zero header
	// without making the map; wire.Nil must leave that header nil.
	return unsafe.Pointer(new(M))
}

func (typedMapOperations[M, K, V]) ensure(address unsafe.Pointer) {
	m := (*M)(address)
	if *m == nil {
		*m = make(M)
	}
}

func (typedMapOperations[M, K, V]) cells() (unsafe.Pointer, unsafe.Pointer) {
	// Hooks may retain either address. Nonzero-sized cells are distinct;
	// zero-sized types retain Go's permitted shared-address behavior.
	return unsafe.Pointer(new(K)), unsafe.Pointer(new(V))
}

func (typedMapOperations[M, K, V]) insert(address, key, element unsafe.Pointer) {
	(*(*M)(address))[*(*K)(key)] = *(*V)(element)
}

func (typedMapOperations[M, K, V]) assign(destination, source unsafe.Pointer) {
	*(*M)(destination) = *(*M)(source)
}

// RegisterMap registers storage operations for a generated map destination.
// It does not register a wire type. Calls must occur during init, without
// concurrent Load or registration; repeated discovery of an exact type is safe.
// An earlier Load may already have built layout operations. Native map storage
// is selected at each map boundary, so those immutable layouts stay valid.
func RegisterMap[M ~map[K]V, K comparable, V any]() {
	typ := reflect.TypeFor[M]()
	if operationRegistry.maps[typ] != nil {
		return
	}
	if operationRegistry.maps == nil {
		operationRegistry.maps = make(map[reflect.Type]mapOperations)
	}
	operationRegistry.maps[typ] = typedMapOperations[M, K, V]{}
}
