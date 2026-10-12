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
	"sync"

	"gvisor.dev/gvisor/pkg/state/wire"
)

// typeOperations is one immutable operation description. Child references may
// form cycles, but no partially built graph is visible to a loader.
// Physical layout is independent of the schema: an ignored field need not
// have decoding operations merely because an interior pointer traverses it.
type typeOperations struct {
	layout *typeLayout
	key    *typeOperations
	elem   *typeOperations
	decode func(*decodeState, *objectDecodeState, location, wire.Object)
	mapOps *mapOperations
	slice  *sliceOperations
}

// newLocation retains the already-selected operations for a fresh Go cell.
func (ops *typeOperations) newLocation() location {
	return location{ops: ops, address: reflect.New(ops.layout.typ).UnsafePointer()}
}

var operationRegistry = struct {
	sync.Mutex
	cache  sync.Map // map[reflect.Type]*typeOperations
	fields sync.Map // map[reflect.Type][]fieldOperation, keyed by receiver pointer type
}{}

// Definitions are registered during init, like wire schemas. An earlier init
// function may already have used a type: its published operations remain valid
// and immutable, retaining their reflection adapters. Later native discovery
// affects only descriptors that have not yet been built. No cache generations
// or concurrent registration contract are introduced.

// operationBuilder resolves recursive types within one private transaction.
// Recursive construction never reenters operationsFor or its lock.
type operationBuilder struct {
	staged map[reflect.Type]*typeOperations
}

func (b *operationBuilder) forType(typ reflect.Type) *typeOperations {
	if ops, ok := operationRegistry.cache.Load(typ); ok {
		return ops.(*typeOperations)
	}
	if ops := b.staged[typ]; ops != nil {
		return ops
	}
	ops := &typeOperations{layout: layoutFor(typ), decode: decoderForKind(typ.Kind())}
	b.staged[typ] = ops
	if definition := reverseTypeDatabase[typ]; definition != nil {
		ops.mapOps = definition.mapOps
		ops.slice = definition.slice
	}
	switch typ.Kind() {
	case reflect.Map:
		ops.key = b.forType(typ.Key())
		ops.elem = b.forType(typ.Elem())
		if ops.mapOps == nil {
			ops.mapOps = reflectedMapOperations
		}
	case reflect.Ptr, reflect.Slice, reflect.Array:
		ops.elem = b.forType(typ.Elem())
		if typ.Kind() == reflect.Slice && ops.slice == nil {
			ops.slice = reflectedSliceOperations
		}
	}

	return ops
}

func operationsFor(typ reflect.Type) *typeOperations {
	if ops, ok := operationRegistry.cache.Load(typ); ok {
		return ops.(*typeOperations)
	}
	operationRegistry.Lock()
	defer operationRegistry.Unlock()
	if ops, ok := operationRegistry.cache.Load(typ); ok {
		return ops.(*typeOperations)
	}
	b := operationBuilder{staged: make(map[reflect.Type]*typeOperations)}
	ops := b.forType(typ)
	b.publish()
	return ops
}

// publish makes a complete private graph visible while the registry is locked.
func (b *operationBuilder) publish() {
	for typ, built := range b.staged {
		operationRegistry.cache.Store(typ, built)
	}
}
