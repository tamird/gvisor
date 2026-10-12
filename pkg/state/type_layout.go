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

// physicalLayouts contains only immutable Go type properties. Unlike capture
// operations, these cannot change when state registrations are added.
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

// find searches for the exact target type at offset. A failed branch appends
// nothing. Successful paths use the existing reverse wire.Dot order.
func (root *typeLayout) find(target *typeLayout, offset uintptr, dots *[]wire.Dot) bool {
	if offset > root.size || target.size > root.size-offset {
		return false
	}
	if root.typ == target.typ && offset == 0 {
		return true
	}
	switch root.kind {
	case reflect.Struct:
		for i := len(root.fields); i > 0; i-- {
			field := &root.fields[i-1]
			// Zero-sized fields may share an offset with a different type.
			if field.offset <= offset && field.layout.find(target, offset-field.offset, dots) {
				if dots != nil {
					// The saved path owns its mutable wire value; do not expose
					// a pointer into globally shared layout metadata.
					name := wire.FieldName(field.name)
					*dots = append(*dots, &name)
				}
				return true
			}
		}
	case reflect.Array:
		if root.length == 0 {
			return false
		}
		index := uintptr(0)
		if size := root.elem.size; size != 0 {
			index, offset = offset/size, offset%size
		}
		// Zero-sized elements share an address. Element zero preserves the
		// same legal pointer identity without division by zero.
		if index < root.length && root.elem.find(target, offset, dots) {
			if dots != nil {
				*dots = append(*dots, wire.Index(index))
			}
			return true
		}
	}
	return false
}

// zeroEncoding follows physical arrays only. It never reads a partially built
// operations descriptor through a recursive map or pointer edge.
func (layout *typeLayout) zeroEncoding() (bool, reflect.Type) {
	switch layout.kind {
	case reflect.Array:
		return layout.elem.zeroEncoding()
	case reflect.Struct:
		return false, nil
	case reflect.Ptr, reflect.Map, reflect.Slice, reflect.Interface,
		reflect.Bool, reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64,
		reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr,
		reflect.Float32, reflect.Float64, reflect.Complex64, reflect.Complex128, reflect.String:
		return true, nil
	default:
		return false, layout.typ
	}
}
