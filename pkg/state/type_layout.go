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
)

// typeLayout describes physical containment, independently of StateSave's
// schema. Ignored fields and foreign codec internals still have a layout;
// inspecting it neither requires a saver nor reads a value.
type typeLayout struct {
	typ    reflect.Type
	size   uintptr
	kind   reflect.Kind
	length uintptr
	elem   *typeLayout
	fields []layoutField
}

type layoutField struct {
	name   string
	offset uintptr
	layout *typeLayout
}

// physicalLayouts contains immutable Go containment properties, independent
// of state registration and decoder operation discovery.
var physicalLayouts sync.Map // map[reflect.Type]*typeLayout

func layoutFor(typ reflect.Type) *typeLayout {
	if layout, ok := physicalLayouts.Load(typ); ok {
		return layout.(*typeLayout)
	}
	layout := &typeLayout{typ: typ, size: typ.Size(), kind: typ.Kind()}
	switch layout.kind {
	case reflect.Struct:
		layout.fields = make([]layoutField, typ.NumField())
		for i := range layout.fields {
			field := typ.Field(i)
			layout.fields[i] = layoutField{
				name:   field.Name,
				offset: field.Offset,
				layout: layoutFor(field.Type),
			}
		}
	case reflect.Array:
		layout.length = uintptr(typ.Len())
		layout.elem = layoutFor(typ.Elem())
	}
	// Physical containment follows only struct fields and array elements. Go
	// types cannot form a cycle without indirection, so no partially built
	// layout needs to be published to terminate this recursion.
	actual, _ := physicalLayouts.LoadOrStore(typ, layout)
	return actual.(*typeLayout)
}
