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
	length     func(valueHandle) int
	isNil      func(valueHandle) bool
	ensure     func(valueHandle)
	newKey     func(valueHandle) valueHandle
	newValue   func(valueHandle) valueHandle
	insert     func(valueHandle, valueHandle, valueHandle)
	visit      func(valueHandle, valueHandle, valueHandle, func(valueHandle, valueHandle))
	key, value func() *valueOperations
}

var reflectedMap = mapStorage{
	length: func(v valueHandle) int { return v.reflected().Len() },
	isNil:  func(v valueHandle) bool { return v.reflected().IsNil() },
	ensure: func(v valueHandle) {
		object := v.reflected()
		if object.IsNil() {
			object.Set(reflect.MakeMap(object.Type()))
		}
	},
	newKey: func(v valueHandle) valueHandle {
		return reflectedHandle(reflect.New(v.Type().Key()).Elem())
	},
	newValue: func(v valueHandle) valueHandle {
		return reflectedHandle(reflect.New(v.Type().Elem()).Elem())
	},
	insert: func(v, key, value valueHandle) {
		v.reflected().SetMapIndex(key.reflected(), value.reflected())
	},
	visit: func(v, key, value valueHandle, visit func(valueHandle, valueHandle)) {
		iter := v.reflected().MapRange()
		for iter.Next() {
			key.reflected().SetIterKey(iter)
			value.reflected().SetIterValue(iter)
			visit(key, value)
		}
	},
}

// sequenceView describes one array traversal. The element accessor for a live
// slice reads its header each time, because an earlier element hook can replace
// that header. Fixed backing arrays instead retain a GC-visible base pointer.
type sequenceView struct {
	object  valueHandle
	base    unsafe.Pointer
	length  int
	element *valueOperations
	index   func(sequenceView, int) valueHandle
}

func reflectedSequence(v valueHandle) sequenceView {
	return sequenceView{object: v, length: v.reflected().Len(), index: reflectedIndex}
}

func reflectedIndex(v sequenceView, i int) valueHandle {
	return reflectedHandle(v.object.reflected().Index(i))
}

// There is one immutable table per reflected kind, independent of registered
// types, checkpoint schemas and Load calls. Typed storage uses the same save
// and load functions with different accessors.
var reflectedOperations [reflect.UnsafePointer + 1]valueOperations

func init() {
	tables := &reflectedOperations
	for kind := reflect.Invalid; kind <= reflect.UnsafePointer; kind++ {
		tables[kind] = valueOperations{
			kind: kind, zero: reflectedZero, copy: reflectedCopy,
			bool: reflectedBool, int: reflectedInt, uint: reflectedUint,
			float: reflectedFloat, cmplx: reflectedComplex, text: reflectedString,
			setBool: reflectedSetBool, setInt: reflectedSetInt, setUint: reflectedSetUint,
			setFloat: reflectedSetFloat, setComplex: reflectedSetComplex, setString: reflectedSetString,
		}
	}
	tables[reflect.Bool].save, tables[reflect.Bool].load = saveBool, loadBool
	for _, kind := range [...]reflect.Kind{reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64} {
		tables[kind].save, tables[kind].load = saveInt, loadInt
	}
	for _, kind := range [...]reflect.Kind{reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr} {
		tables[kind].save, tables[kind].load = saveUint, loadUint
	}
	for _, kind := range [...]reflect.Kind{reflect.Float32, reflect.Float64} {
		tables[kind].save, tables[kind].load = saveFloat, loadFloat
	}
	for _, kind := range [...]reflect.Kind{reflect.Complex64, reflect.Complex128} {
		tables[kind].save, tables[kind].load = saveComplex, loadComplex
	}
	tables[reflect.String].save, tables[reflect.String].load = saveString, loadString
	tables[reflect.Pointer].save, tables[reflect.Pointer].load = savePointer, loadPointer
	tables[reflect.Map].save, tables[reflect.Map].load = saveMap, loadMap
	tables[reflect.Map].mapStorage = &reflectedMap
	tables[reflect.Slice].save, tables[reflect.Slice].load = saveSlice, loadSlice
	tables[reflect.Slice].sequence = reflectedSequence
	tables[reflect.Array].save, tables[reflect.Array].load = saveArray, loadArray
	tables[reflect.Array].sequence = reflectedSequence
	tables[reflect.Struct].save, tables[reflect.Struct].load = saveStruct, loadStruct
	tables[reflect.Interface].save = saveInterface
}

func typedZero[T any](v valueHandle) { var zero T; *v.storage.(*T) = zero }
func typedCopy[T any](v valueHandle) valueHandle {
	copy := new(T)
	*copy = *v.storage.(*T)
	return typedHandle(copy, v.ops)
}

// The constructors below are used only by the bounded manual prototype. They
// produce flat tables once; no constructor runs while saving or loading.
func integerOperations[T ~int | ~int8 | ~int16 | ~int32 | ~int64]() valueOperations {
	return valueOperations{
		kind: reflect.TypeFor[T]().Kind(), save: saveInt, load: loadInt,
		zero: typedZero[T], copy: typedCopy[T],
		int: func(v valueHandle) int64 { return int64(*v.storage.(*T)) },
		setInt: func(v valueHandle, x int64) int64 {
			p := v.storage.(*T)
			*p = T(x)
			return int64(*p)
		},
	}
}

func mapOperations[M ~map[K]V, K comparable, V any](key, value func() *valueOperations) valueOperations {
	return valueOperations{
		kind: reflect.Map, save: saveMap, load: loadMap,
		zero: typedZero[M], copy: typedCopy[M],
		mapStorage: &mapStorage{
			key: key, value: value,
			length: func(v valueHandle) int { return len(*v.storage.(*M)) },
			isNil:  func(v valueHandle) bool { return *v.storage.(*M) == nil },
			ensure: func(v valueHandle) {
				p := v.storage.(*M)
				if *p == nil {
					*p = make(M)
				}
			},
			newKey:   func(v valueHandle) valueHandle { return typedHandle(new(K), v.ops.mapStorage.key()) },
			newValue: func(v valueHandle) valueHandle { return typedHandle(new(V), v.ops.mapStorage.value()) },
			insert:   func(v, k, x valueHandle) { (*v.storage.(*M))[*k.storage.(*K)] = *x.storage.(*V) },
			visit: func(v, k, x valueHandle, visit func(valueHandle, valueHandle)) {
				for key, value := range *v.storage.(*M) {
					*k.storage.(*K), *x.storage.(*V) = key, value
					visit(k, x)
				}
			},
		},
	}
}

func sliceOperations[S ~[]E, E any](element, backing func() *valueOperations) valueOperations {
	return valueOperations{kind: reflect.Slice, save: saveSlice, load: loadSlice,
		zero: typedZero[S], copy: typedCopy[S], element: element, backing: backing,
		sequence: func(v valueHandle) sequenceView {
			return sequenceView{object: v, length: len(*v.storage.(*S)), element: v.ops.element(), index: sliceIndex[S, E]}
		}}
}

func sliceIndex[S ~[]E, E any](v sequenceView, i int) valueHandle {
	return typedHandle(&(*v.object.storage.(*S))[i], v.element)
}

func backingOperations[E any](element func() *valueOperations) valueOperations {
	return valueOperations{kind: reflect.Array, save: saveArray, load: loadArray,
		zero: reflectedZero, copy: reflectedCopy, element: element,
		sequence: func(v valueHandle) sequenceView {
			object := v.reflected()
			if object.Kind() != reflect.Array || object.Type().Elem() != reflect.TypeFor[E]() {
				Failf("backing array %s has incompatible element type %s", object.Type(), reflect.TypeFor[E]())
			}
			return sequenceView{base: object.Addr().UnsafePointer(), length: object.Len(),
				element: v.ops.element(), index: backingIndex[E]}
		}}
}

func backingIndex[E any](v sequenceView, i int) valueHandle {
	var element E
	// The view was made from an addressable [N]E at a graph boundary. Its
	// pointer keeps the allocation reachable, including zero-sized elements.
	return typedHandle((*E)(unsafe.Add(v.base, uintptr(i)*unsafe.Sizeof(element))), v.element)
}
