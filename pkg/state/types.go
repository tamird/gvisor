// Copyright 2020 The gVisor Authors.
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
	"encoding"
	"reflect"
	"slices"
	"time"
)

// interfaceType is the name of the empty interface type.
const interfaceType = "interface"

// primitiveTypeDatabase is a set of fixed types.
var primitiveTypeDatabase = func() map[string]reflect.Type {
	r := make(map[string]reflect.Type)
	for _, t := range []reflect.Type{
		reflect.TypeFor[bool](),
		reflect.TypeFor[int](),
		reflect.TypeFor[int8](),
		reflect.TypeFor[int16](),
		reflect.TypeFor[int32](),
		reflect.TypeFor[int64](),
		reflect.TypeFor[uint](),
		reflect.TypeFor[uintptr](),
		reflect.TypeFor[uint8](),
		reflect.TypeFor[uint16](),
		reflect.TypeFor[uint32](),
		reflect.TypeFor[uint64](),
		reflect.TypeFor[string](),
		reflect.TypeFor[float32](),
		reflect.TypeFor[float64](),
		reflect.TypeFor[complex64](),
		reflect.TypeFor[complex128](),
	} {
		r[t.Name()] = t
	}
	return r
}()

// registeredType is a registered type.
type registeredType struct {
	typ reflect.Type
	// noHooks is true for structs registered with RegisterNoHooks.
	noHooks bool
}

// globalTypeDatabase maps registered type names to types. It is used to find
// the types of objects and of the values of interfaces on Load.
var globalTypeDatabase = map[string]registeredType{}

// registeredTypeOf returns the type registered with name, or nil.
func registeredTypeOf(name string) reflect.Type {
	return globalTypeDatabase[name].typ
}

// binaryTypes maps types registered with registerBinary to their names.
var binaryTypes = map[reflect.Type]string{}

// Release releases references to global type databases.
// Must only be called in contexts where they will definitely never be used,
// in order to save memory.
func Release() {
	globalTypeDatabase = nil
	binaryTypes = nil
}

// binaryObject is a value with a self-contained binary representation.
type binaryObject interface {
	encoding.BinaryAppender
	encoding.BinaryUnmarshaler
}

// eagerTypeInfo controls whether `Register` validates the metadata of types.
// `Register` runs in `init` on every process start, this is on the startup
// critical path. Enable it only in race-enabled builds. Otherwise, types are
// validated when first saved or loaded.
var eagerTypeInfo = raceEnabled

// Register registers a type.
//
// This must be called on init and only done once. Registration lets Load
// find the concrete types of objects and of values held in interfaces.
func Register(t Type) {
	register(t, false)
}

// StructType is implemented by pointers to struct types with state methods.
type StructType interface {
	Type
	SaverLoader
}

// RegisterStruct registers a struct type, like Register. Passing the type as
// a StructType makes the interface table of StructType for the type static,
// so that the state package does not build it at run time.
//
// This must be called on init and only done once.
func RegisterStruct(t StructType) {
	registerStruct(t, false)
}

// RegisterNoHooks registers a struct type whose StateSave and StateLoad
// methods save and load its fields, and do nothing else, like RegisterStruct.
// The state package may save values of the type without calling these
// methods.
//
// This must be called on init and only done once.
func RegisterNoHooks(t StructType) {
	registerStruct(t, true)
}

func registerStruct(t StructType, noHooks bool) {
	if eagerTypeInfo {
		register(t, noHooks)
		return
	}
	// Unlike register, this does not convert t to Type, which would look up
	// an itab at run time.
	registerName(reflect.TypeOf(t).Elem(), t.StateTypeName(), noHooks)
}

func register(t Type, noHooks bool) {
	typ := reflect.TypeOf(t)
	if !eagerTypeInfo {
		registerName(typ.Elem(), t.StateTypeName(), noHooks)
		return
	}
	// Register must always be called on pointers.
	if typ.Kind() != reflect.Ptr {
		Failf("Register must be called on pointers")
	}
	typ = typ.Elem()
	fields := t.StateFields()
	if typ.Kind() == reflect.Struct {
		// All registered structs must implement SaverLoader. We allow
		// the registration is non-struct types with just the Type
		// interface, but we need to call StateSave/StateLoad methods
		// on aggregate types.
		if _, ok := t.(SaverLoader); !ok {
			Failf("struct %T does not implement SaverLoader", t)
		}
	} else {
		// Non-structs must not have any fields. We don't support
		// calling StateSave/StateLoad methods on any non-struct types.
		// If custom behavior is required, these types should be
		// wrapped in a structure of some kind.
		if len(fields) != 0 {
			Failf("non-struct %T has non-zero fields %v", t, fields)
		}
		// We don't allow non-structs to implement StateSave/StateLoad
		// methods, because they won't be called and it's confusing.
		if _, ok := t.(SaverLoader); ok {
			Failf("non-struct %T implements SaverLoader", t)
		}
	}
	validateTypeInfo(t.StateTypeName(), fields)
	registerName(typ, t.StateTypeName(), noHooks)
}

// registerBinary registers a named foreign struct once, retaining its original
// type for fields, map keys and interfaces. value must point to a named struct.
func registerBinary(value binaryObject) {
	typ := reflect.TypeOf(value).Elem()
	name := typ.PkgPath() + "." + typ.Name()
	registerName(typ, name, false)
	binaryTypes[typ] = name
}

// registerName publishes `typ` but does not validate.
func registerName(typ reflect.Type, name string, noHooks bool) {
	if raceEnabled {
		if _, ok := primitiveTypeDatabase[name]; ok {
			Failf("conflicting primitiveTypeDatabase entry for %v: used by primitive", typ)
		}
		if _, ok := globalTypeDatabase[name]; ok {
			Failf("conflicting globalTypeDatabase entries for %v: name conflict", typ)
		}
		if name == interfaceType {
			Failf("conflicting name for %v: matches interfaceType", typ)
		}
	}
	globalTypeDatabase[name] = registeredType{typ: typ, noHooks: noHooks}
	fingerprint.Store(0)
}

// validateTypeInfo checks that a type has a name and unique, non-empty field
// names.
func validateTypeInfo(name string, fields []string) {
	if name == "" {
		Failf("type has empty name")
	}
	for i, field := range fields {
		if field == "" {
			Failf("field has empty name for type %q", name)
		}
		if slices.Contains(fields[:i], field) {
			Failf("duplicate field %q for type %s", field, name)
		}
	}
}

func init() {
	registerBinary((*time.Time)(nil))
}
