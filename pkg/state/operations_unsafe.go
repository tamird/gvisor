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
	codec  nativeCodec
}

// newLocation retains the already-selected operations for a fresh Go cell.
func (ops *typeOperations) newLocation() location {
	if ops.codec.newValue != nil {
		return location{ops: ops, address: ops.codec.newValue()}
	}
	return location{ops: ops, address: reflect.New(ops.layout.typ).UnsafePointer()}
}

// newCell keeps the reflection value only where map insertion or interface
// assignment requires it. Every nonzero-sized cell is a fresh allocation;
// zero-sized allocations retain Go's permitted shared-address behavior.
func (ops *typeOperations) newCell() (location, reflect.Value) {
	if ops.codec.newValue != nil {
		obj := ops.newLocation()
		return obj, obj.reflect()
	}
	value := reflect.New(ops.layout.typ).Elem()
	return location{ops: ops, address: value.Addr().UnsafePointer()}, value
}

var operationRegistry = struct {
	sync.Mutex
	cache sync.Map // map[reflect.Type]*typeOperations
}{}

// operationBuilder resolves recursive types within one private transaction.
// Physical layouts depend on Go types; native callbacks snapshot exact-type
// registration when this graph is built. An early-init fallback stays immutable
// after later registration. Publication waits for the complete recursive graph,
// and construction never reenters operationsFor or its lock.
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
	if info, ok := reverseTypeDatabase[typ]; ok {
		if registered, ok := globalTypeDatabase[info.Name]; ok && registered.typ == typ {
			ops.codec = registered.codec
		}
	}
	switch typ.Kind() {
	case reflect.Map:
		ops.key = b.forType(typ.Key())
		ops.elem = b.forType(typ.Elem())
	case reflect.Ptr, reflect.Slice, reflect.Array:
		ops.elem = b.forType(typ.Elem())
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
