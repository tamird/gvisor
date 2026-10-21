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

// DecoderTypes holds the destination operations for one Load. Generated
// StateDecodeTypes methods supply Go operations for statically known types;
// types encountered only through interfaces use reflected operations. Both
// supply storage to the same decoder and graph resolver.
//
// Construction is lazy so importing a package does not build decoder tables.
type DecoderTypes struct {
	byType map[reflect.Type]*valueDecoder
}

type valueDecoder struct {
	initialized bool
	typ         reflect.Type
	new         func() any
	zero        func(any)
	load        func(*decodeState, *objectDecodeState, any, wire.Object)
	// index addresses an element of this type in a contiguous array. The
	// array decoder checks the length before invoking it.
	index func(unsafe.Pointer, int) any
	// Primitive arrays have no hooks and can be restored in bulk.
	array func(unsafe.Pointer, int, wire.Array)
}

func (d *valueDecoder) restore(ds *decodeState, ods *objectDecodeState, pointer any, encoded wire.Object) {
	value := dataValue(encoded)
	if _, ok := value.(wire.Nil); ok {
		return
	}
	if !d.initialized {
		d.initialized = true
		// Allocation alone must not call promoted methods: a referenced
		// object can have a Nil body and an uninitialized embedded receiver.
		if factory, ok := pointer.(interface{ StateDecodeTypes(*DecoderTypes) }); ok {
			factory.StateDecodeTypes(&ds.decoders)
		}
	}
	if wrapped, ok := value.(*wire.Interface); ok && d.typ.Kind() != reflect.Interface {
		// Manual loaders may unwrap a dynamic interface into its concrete
		// type. Go's assignment check still applies at this boundary.
		ds.decodeInterface(ods, reflect.ValueOf(pointer).Elem(), wrapped)
		return
	}
	d.load(ds, ods, pointer, value)
}

func (types *DecoderTypes) lookup(typ reflect.Type) *valueDecoder {
	if d := types.byType[typ]; d != nil {
		return d
	}
	if types.byType == nil {
		types.byType = make(map[reflect.Type]*valueDecoder)
	}
	d := &valueDecoder{typ: typ}
	// Publish the cell before discovering children: a struct may contain a
	// pointer, slice or map that leads back to the struct. Factories update
	// this cell rather than replacing references already held by parents.
	types.byType[typ] = d
	d.new = func() any { return reflect.New(typ).Interface() }
	d.zero = func(pointer any) { reflect.ValueOf(pointer).Elem().SetZero() }
	d.index = func(base unsafe.Pointer, i int) any {
		return reflect.NewAt(typ, unsafe.Add(base, uintptr(i)*typ.Size())).Interface()
	}
	types.configure(d)
	return d
}

func (types *DecoderTypes) configure(d *valueDecoder) {
	typ := d.typ
	switch typ.Kind() {
	case reflect.Bool:
		d.load = boolDecoder(func(p any, v bool) { reflect.ValueOf(p).Elem().SetBool(v) })
		d.array = boolArray[bool]
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		d.load = intDecoder(func(p any, v int64) int64 {
			target := reflect.ValueOf(p).Elem()
			target.SetInt(v)
			return target.Int()
		})
		switch typ.Kind() {
		case reflect.Int:
			d.array = intArray[int]
		case reflect.Int8:
			d.array = intArray[int8]
		case reflect.Int16:
			d.array = intArray[int16]
		case reflect.Int32:
			d.array = intArray[int32]
		case reflect.Int64:
			d.array = intArray[int64]
		}
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
		d.load = uintDecoder(func(p any, v uint64) uint64 {
			target := reflect.ValueOf(p).Elem()
			target.SetUint(v)
			return target.Uint()
		})
		switch typ.Kind() {
		case reflect.Uint:
			d.array = uintArray[uint]
		case reflect.Uint8:
			d.array = uintArray[uint8]
		case reflect.Uint16:
			d.array = uintArray[uint16]
		case reflect.Uint32:
			d.array = uintArray[uint32]
		case reflect.Uint64:
			d.array = uintArray[uint64]
		case reflect.Uintptr:
			d.array = uintArray[uintptr]
		}
	case reflect.Float32, reflect.Float64:
		d.load = floatDecoder(func(p any, v float64) float64 {
			target := reflect.ValueOf(p).Elem()
			target.SetFloat(v)
			return target.Float()
		})
		if typ.Kind() == reflect.Float32 {
			d.array = floatArray[float32]
		} else {
			d.array = floatArray[float64]
		}
	case reflect.Complex64, reflect.Complex128:
		d.load = complexDecoder(func(p any, v complex128) complex128 {
			target := reflect.ValueOf(p).Elem()
			target.SetComplex(v)
			return target.Complex()
		})
		if typ.Kind() == reflect.Complex64 {
			d.array = complexArray[complex64]
		} else {
			d.array = complexArray[complex128]
		}
	case reflect.String:
		d.load = stringDecoder(func(p any, v string) { reflect.ValueOf(p).Elem().SetString(v) })
		d.array = stringArray[string]
	case reflect.Pointer:
		d.load = func(ds *decodeState, _ *objectDecodeState, p any, value wire.Object) {
			ds.restoreReference(reflect.ValueOf(p).Elem(), requireWire[*wire.Ref](value))
		}
	case reflect.Struct:
		d.load = func(ds *decodeState, ods *objectDecodeState, p any, value wire.Object) {
			ds.decodeStruct(ods, p, typ, requireWire[*wire.Struct](value))
		}
	case reflect.Interface:
		d.load = func(ds *decodeState, ods *objectDecodeState, p any, value wire.Object) {
			ds.decodeInterface(ods, reflect.ValueOf(p).Elem(), requireWire[*wire.Interface](value))
		}
	case reflect.Map:
		key, value := types.lookup(typ.Key()), types.lookup(typ.Elem())
		d.load = mapDecoder(key, value,
			func(p any) {
				target := reflect.ValueOf(p).Elem()
				if target.IsNil() {
					target.Set(reflect.MakeMap(typ))
				}
			},
			func(p, k, v any) {
				reflect.ValueOf(p).Elem().SetMapIndex(reflect.ValueOf(k).Elem(), reflect.ValueOf(v).Elem())
			})
	case reflect.Array:
		element := types.lookup(typ.Elem())
		d.load = func(ds *decodeState, ods *objectDecodeState, p any, value wire.Object) {
			target := reflect.ValueOf(p).Elem()
			ds.restoreArray(ods, element, target.Len(), func(i int) any {
				return element.index(target.Addr().UnsafePointer(), i)
			}, func() unsafe.Pointer { return target.Addr().UnsafePointer() }, requireWire[wire.Array](value))
		}
	case reflect.Slice:
		element := types.lookup(typ.Elem())
		d.load = sliceDecoder(element, func(p any) int { return reflect.ValueOf(p).Elem().Len() },
			func(p any, i int) any { return reflect.ValueOf(p).Elem().Index(i).Addr().Interface() },
			func(p any) unsafe.Pointer { return reflect.ValueOf(p).Elem().Index(0).Addr().UnsafePointer() })
	default:
		d.load = func(_ *decodeState, _ *objectDecodeState, _ any, value wire.Object) {
			Failf("cannot restore %T into %s", value, typ)
		}
	}
}

// AddValue supplies typed storage for a type. Structs keep their existing
// StateLoad method; dynamic references still use the graph resolver.
func AddValue[T any](types *DecoderTypes, _ *T) {
	d := types.lookup(reflect.TypeFor[T]())
	d.new = func() any { return new(T) }
	d.zero = func(p any) { var zero T; *p.(*T) = zero }
	d.index = func(base unsafe.Pointer, i int) any {
		var element T
		return (*T)(unsafe.Add(base, uintptr(i)*unsafe.Sizeof(element)))
	}
}

// AddBool supplies typed bool stores.
func AddBool[T ~bool](types *DecoderTypes, sample *T) {
	AddValue(types, sample)
	d := types.lookup(reflect.TypeFor[T]())
	d.load = boolDecoder(func(p any, v bool) { *p.(*T) = T(v) })
	d.array = boolArray[T]
}

// AddInt supplies typed signed integer stores.
func AddInt[T ~int | ~int8 | ~int16 | ~int32 | ~int64](types *DecoderTypes, sample *T) {
	AddValue(types, sample)
	d := types.lookup(reflect.TypeFor[T]())
	d.load = intDecoder(func(p any, v int64) int64 { *p.(*T) = T(v); return int64(*p.(*T)) })
	d.array = intArray[T]
}

// AddUint supplies typed unsigned integer stores.
func AddUint[T ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr](types *DecoderTypes, sample *T) {
	AddValue(types, sample)
	d := types.lookup(reflect.TypeFor[T]())
	d.load = uintDecoder(func(p any, v uint64) uint64 { *p.(*T) = T(v); return uint64(*p.(*T)) })
	d.array = uintArray[T]
}

// AddFloat supplies typed floating point stores.
func AddFloat[T ~float32 | ~float64](types *DecoderTypes, sample *T) {
	AddValue(types, sample)
	d := types.lookup(reflect.TypeFor[T]())
	d.load = floatDecoder(func(p any, v float64) float64 { *p.(*T) = T(v); return float64(*p.(*T)) })
	d.array = floatArray[T]
}

// AddComplex supplies typed complex number stores.
func AddComplex[T ~complex64 | ~complex128](types *DecoderTypes, sample *T) {
	AddValue(types, sample)
	d := types.lookup(reflect.TypeFor[T]())
	d.load = complexDecoder(func(p any, v complex128) complex128 { *p.(*T) = T(v); return complex128(*p.(*T)) })
	d.array = complexArray[T]
}

// AddString supplies typed string stores.
func AddString[T ~string](types *DecoderTypes, sample *T) {
	AddValue(types, sample)
	d := types.lookup(reflect.TypeFor[T]())
	d.load = stringDecoder(func(p any, v string) { *p.(*T) = T(v) })
	d.array = stringArray[T]
}

// AddMap supplies typed allocation and insertion to the shared map decoder.
func AddMap[M ~map[K]V, K comparable, V any](types *DecoderTypes, sample *M) {
	AddValue(types, sample)
	AddValue(types, (*K)(nil))
	AddValue(types, (*V)(nil))
	types.lookup(reflect.TypeFor[M]()).load = mapDecoder(types.lookup(reflect.TypeFor[K]()), types.lookup(reflect.TypeFor[V]()),
		func(p any) {
			if *p.(*M) == nil {
				*p.(*M) = make(M)
			}
		},
		func(p, key, value any) { (*p.(*M))[*key.(*K)] = *value.(*V) })
}

// AddSlice supplies access through the live slice header, including when an
// element's StateLoad replaces that header.
func AddSlice[S ~[]E, E any](types *DecoderTypes, sample *S) {
	AddValue(types, sample)
	AddValue(types, (*E)(nil))
	types.lookup(reflect.TypeFor[S]()).load = sliceDecoder(types.lookup(reflect.TypeFor[E]()),
		func(p any) int { return len(*p.(*S)) },
		func(p any, i int) any { return &(*p.(*S))[i] },
		func(p any) unsafe.Pointer { return unsafe.Pointer(&(*p.(*S))[0]) })
}

// AddArray supplies typed array storage and element addresses.
func AddArray[A any, E any](types *DecoderTypes, sample *A, _ []E) {
	typ := reflect.TypeFor[A]()
	if typ.Kind() != reflect.Array || typ.Elem() != reflect.TypeFor[E]() {
		panic("AddArray requires an array and a slice of its element type")
	}
	AddValue(types, sample)
	AddValue(types, (*E)(nil))
	element := types.lookup(reflect.TypeFor[E]())
	types.lookup(typ).load = func(ds *decodeState, ods *objectDecodeState, p any, value wire.Object) {
		base := unsafe.Pointer(p.(*A))
		ds.restoreArray(ods, element, typ.Len(), func(i int) any { return element.index(base, i) },
			func() unsafe.Pointer { return base }, requireWire[wire.Array](value))
	}
}

// AddPointer supplies typed pointee storage; reference assignment retains the
// ordinary Go assignability checks, including defined pointer types.
func AddPointer[P ~*E, E any](types *DecoderTypes, sample *P) {
	AddValue(types, sample)
	AddValue(types, (*E)(nil))
}

// MapTypes returns zero values for a map's key and value types, allowing
// generated code to describe unexported imported types without naming them.
func MapTypes[M ~map[K]V, K comparable, V any](_ M) (key K, value V) { return }

// SliceTypes returns a zero value of a slice's element type.
func SliceTypes[S ~[]E, E any](_ S) (element E) { return }

// PointerTypes returns a zero value of a pointer's element type.
func PointerTypes[P ~*E, E any](_ P) (element E) { return }
