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
	"context"
	"reflect"
	"sync"
	"unsafe"
)

// valueMemory owns allocation and copying for an exact Go type. Generated
// registration supplies native operations; the public untyped registration
// boundary can retain reflected operations for types it cannot name.
type valueMemory struct {
	newValue  func() reflect.Value
	copyValue func(reflect.Value) reflect.Value
	save      func(unsafe.Pointer, string, Sink)
	load      func(unsafe.Pointer, context.Context, Source)
}

func memoryFor[T any]() *valueMemory {
	memory := &valueMemory{
		newValue: func() reflect.Value {
			return reflect.ValueOf(new(T)).Elem()
		},
		copyValue: func(source reflect.Value) reflect.Value {
			value := new(T)
			*value = source.Interface().(T)
			return reflect.ValueOf(value).Elem()
		},
	}
	// Select the contract once. SaverLoader retains precedence over a foreign
	// binary codec, including when methods are promoted from an embedded type.
	switch any((*T)(nil)).(type) {
	case SaverLoader:
		memory.save = func(p unsafe.Pointer, _ string, s Sink) { any((*T)(p)).(SaverLoader).StateSave(s) }
		memory.load = func(p unsafe.Pointer, ctx context.Context, s Source) { any((*T)(p)).(SaverLoader).StateLoad(ctx, s) }
	case binaryObject:
		memory.save = func(p unsafe.Pointer, name string, s Sink) {
			s.internal.es.saveBinary(any((*T)(p)).(binaryObject), name, s)
		}
		memory.load = func(p unsafe.Pointer, _ context.Context, s Source) {
			loadBinary(any((*T)(p)).(binaryObject), s.internal.rte.Name, s)
		}
	}
	return memory
}

// typeOperations is one immutable operation description. Child references may
// form cycles, but no partially built graph is visible to a save or load.
// Physical layout is separate from capture support: an ignored field does not
// require a saver merely because an interior pointer traverses its storage.
type typeOperations struct {
	layout          *typeLayout
	memory          *valueMemory
	key             *typeOperations
	elem            *typeOperations
	primitiveZero   bool
	zeroUnsupported reflect.Type
	mapOps          *mapOperations
	slice           *sliceOperations
}

// newValue retains the already-selected operations when allocating an entry.
// Its caller need not rediscover the type of a cell it just requested.
func (ops *typeOperations) newValue() value {
	obj := ops.memory.newValue()
	return value{ops: ops, address: obj.Addr().UnsafePointer(), reflected: obj}
}

var operationRegistry = struct {
	sync.Mutex
	cache sync.Map // map[reflect.Type]*typeOperations
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
	ops := &typeOperations{layout: layoutFor(typ)}
	ops.primitiveZero, ops.zeroUnsupported = ops.layout.zeroEncoding()
	b.staged[typ] = ops
	if definition := reverseTypeDatabase[typ]; definition != nil {
		ops.memory = definition.memory
		ops.mapOps = definition.mapOps
		ops.slice = definition.slice
	}
	if ops.memory == nil {
		ops.memory = &valueMemory{
			newValue: func() reflect.Value {
				return reflect.New(typ).Elem()
			},
			copyValue: func(source reflect.Value) reflect.Value {
				value := reflect.New(typ).Elem()
				value.Set(source)
				return value
			},
		}
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
			ops.slice = makeReflectedSliceOperations()
		}
	}

	if typ.Kind() == reflect.Array {
		if definition := reverseTypeDatabase[typ.Elem()]; definition != nil && definition.array != nil {
			allocate := definition.array
			ops.memory = &valueMemory{newValue: func() reflect.Value { return reflect.NewAt(typ, allocate(typ.Len())).Elem() }, copyValue: ops.memory.copyValue}
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
	for typ, built := range b.staged {
		operationRegistry.cache.Store(typ, built)
	}
	return ops
}

// addMemory installs the canonical native operations for a type. Repeated
// discovery through other generated fields is idempotent: every call uses the
// same generic implementation for the exact Go type, not a user-supplied
// function whose identity would need comparison. The registry lock is held.
func addMemory(typ reflect.Type, newMemory func() *valueMemory) {
	definition := reverseTypeDatabase[typ]
	if definition == nil {
		definition = &registeredType{typ: typ}
		reverseTypeDatabase[typ] = definition
	}
	if definition.memory == nil {
		definition.memory = newMemory()
	}
}

func registerMemory[T any]() {
	operationRegistry.Lock()
	defer operationRegistry.Unlock()
	addMemory(reflect.TypeFor[T](), memoryFor[T])
	addMemory(reflect.TypeFor[*T](), memoryFor[*T])
}

func init() {
	registerMemory[bool]()
	registerMemory[int]()
	registerMemory[int8]()
	registerMemory[int16]()
	registerMemory[int32]()
	registerMemory[int64]()
	registerMemory[uint]()
	registerMemory[uint8]()
	registerMemory[uint16]()
	registerMemory[uint32]()
	registerMemory[uint64]()
	registerMemory[uintptr]()
	registerMemory[float32]()
	registerMemory[float64]()
	registerMemory[complex64]()
	registerMemory[complex128]()
	registerMemory[string]()
}
