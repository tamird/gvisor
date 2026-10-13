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

// mapOperations supplies storage for an exact Go map type. Graph ownership,
// entry decoding, dependencies and insertion order stay in the common decoder.
type mapOperations interface {
	newMap() reflect.Value
	ensure(unsafe.Pointer)
	entry(*decodeState, *objectDecodeState, unsafe.Pointer, wire.Object, wire.Object)
	assign(unsafe.Pointer, unsafe.Pointer)
}

type typedMapOperations[M ~map[K]V, K comparable, V any] struct{}

func (typedMapOperations[M, K, V]) newMap() reflect.Value {
	// A pointer or interface may refer to a nil map. The zero header must
	// remain nil if its eventual body is wire.Nil.
	return reflect.ValueOf(new(M))
}

func (typedMapOperations[M, K, V]) ensure(address unsafe.Pointer) {
	m := (*M)(address)
	if *m == nil {
		*m = make(M)
	}
}

func (typedMapOperations[M, K, V]) entry(ds *decodeState, ods *objectDecodeState, address unsafe.Pointer, encodedKey, encodedValue wire.Object) {
	// Allocate both cells before either hook. Escaping receivers remain
	// separate for nonzero-sized values; zero-sized cells follow Go's rules.
	key, value := new(K), new(V)
	ds.decodeMapEntry(ods, reflect.ValueOf(key), reflect.ValueOf(value), encodedKey, encodedValue)
	// Hooks may replace the header, so read it only after decoding both cells.
	(*(*M)(address))[*key] = *value
}

func (typedMapOperations[M, K, V]) assign(destination, source unsafe.Pointer) {
	*(*M)(destination) = *(*M)(source)
}

var mapStorage map[reflect.Type]mapOperations

// RegisterMap registers storage operations for generated map destinations,
// independently of wire names. Calls must occur during init, without concurrent
// Load or registration; repeated discovery of an exact type is safe. An earlier
// Load may use reflection, and later loads select the registered operations.
func RegisterMap[M ~map[K]V, K comparable, V any]() {
	typ := reflect.TypeFor[M]()
	if mapStorage[typ] != nil {
		return
	}
	if mapStorage == nil {
		mapStorage = make(map[reflect.Type]mapOperations)
	}
	mapStorage[typ] = typedMapOperations[M, K, V]{}
}

// newObject returns a pointer to zero storage before the graph publishes it.
// Only map allocation has native storage operations; all other types keep the
// coherent decoder's allocation boundary.
func newObject(typ reflect.Type) reflect.Value {
	if typ.Kind() == reflect.Map {
		if native := mapStorage[typ]; native != nil {
			return native.newMap()
		}
	}
	return reflect.New(typ)
}

func (ds *decodeState) mapTarget(ref *wire.Ref, typ reflect.Type) reflect.Value {
	targetPtr := ds.register(ref, typ)
	target := targetPtr.Elem()
	if target.Kind() != reflect.Map || !target.Type().AssignableTo(typ) {
		Failf("map reference type %v does not match %v", target.Type(), typ)
	}
	// A map reference makes its header usable before the payload is decoded.
	// Pointer and interface allocations remain zero until this boundary.
	if native := mapStorage[target.Type()]; native != nil {
		native.ensure(targetPtr.UnsafePointer())
	} else if target.IsNil() {
		target.Set(reflect.MakeMap(target.Type()))
	}
	return targetPtr
}

// assignMap assigns the initialized map returned by mapTarget. Different
// assignable map types retain Go's checked assignment; only exact types use
// the native store.
func assignMap(destination unsafe.Pointer, typ reflect.Type, targetPtr reflect.Value) {
	target := targetPtr.Elem()
	if native := mapStorage[target.Type()]; native != nil {
		if target.Type() == typ {
			native.assign(destination, targetPtr.UnsafePointer())
			return
		}
	}
	reflect.NewAt(typ, destination).Elem().Set(target)
}
