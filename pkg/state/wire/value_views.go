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

package wire

// ReferenceView reads reference metadata without constructing interface-valued
// Dots or TypeSpecs. Its lifetime is the lifetime of its originating Value.
type ReferenceView struct {
	value Value
}

// PathElement is one decoded interior-pointer step. Field distinguishes a field
// name from an array index, without boxing the index in a Dot interface.
type PathElement struct {
	Index Index
	Name  string
	Field bool
}

// ReferenceView returns a read-only reference view.
func (v Value) ReferenceView() ReferenceView {
	v.require(KindRef)
	return ReferenceView{value: v}
}

// Root returns the full-width encoded object ID.
func (r ReferenceView) Root() Uint { return Uint(r.value.scalarBits()) }

// Dots returns the number of interior-pointer steps.
func (r ReferenceView) Dots() int {
	if frame := r.value.frame(); frame != nil {
		metadata := frame.node(int(r.value.bits)).b
		if metadata == 0 {
			return 0
		}
		return int(frame.node(int(metadata - 1)).kind)
	}
	return len(r.value.Reference().Dots)
}

// Dot returns a path step in the original reverse order.
func (r ReferenceView) Dot(i int) PathElement {
	if frame := r.value.frame(); frame != nil {
		metadata := frame.node(int(r.value.bits)).b
		if metadata == 0 || i < 0 || uint64(i) >= uint64(frame.node(int(metadata-1)).kind) {
			panic("wire path index out of range")
		}
		dot := frame.node(int(frame.node(int(metadata-1)).a) + i)
		if dot.kind != 0 {
			return PathElement{Name: frame.strings[dot.a], Field: true}
		}
		return PathElement{Index: Index(dot.a)}
	}
	switch dot := r.value.Reference().Dots[i].(type) {
	case Index:
		return PathElement{Index: dot}
	case *FieldName:
		return PathElement{Name: string(*dot), Field: true}
	default:
		panic("invalid owned reference path")
	}
}

// Type returns the enclosing type specification, if this reference has Dots.
func (r ReferenceView) Type() TypeView {
	if frame := r.value.frame(); frame != nil {
		metadata := frame.node(int(r.value.bits)).b
		if metadata == 0 {
			return TypeView{}
		}
		return TypeView{frame: frame, index: int(frame.node(int(metadata - 1)).b)}
	}
	return TypeView{owned: r.value.Reference().Type}
}

func (r ReferenceView) materialize() Ref {
	if r.value.frame() == nil {
		return r.value.Reference()
	}
	value := Ref{Root: r.Root()}
	if count := r.Dots(); count != 0 {
		value.Dots = make([]Dot, count) // Escapes: owned reference path.
		for i := range value.Dots {
			dot := r.Dot(i)
			if dot.Field {
				name := new(FieldName) // Escapes: owned field-name interface.
				*name = FieldName(dot.Name)
				value.Dots[i] = name
			} else {
				value.Dots[i] = dot.Index // Escapes: boxing the owned index.
			}
		}
		value.Type = r.Type().materialize()
	}
	return value
}

// SliceView reads a slice header and its reference without materialization.
type SliceView struct {
	Length   Uint
	Capacity Uint
	Ref      ReferenceView
}

// SliceView returns a read-only slice view.
func (v Value) SliceView() SliceView {
	v.require(KindSlice)
	if frame := v.frame(); frame != nil {
		node := frame.node(int(v.bits))
		bounds := frame.node(int(node.a))
		return SliceView{Length: Uint(bounds.a), Capacity: Uint(bounds.b), Ref: frameValue(frame, int(node.b)).ReferenceView()}
	}
	return v.ownedSliceView()
}

func (s SliceView) materialize() Slice {
	return Slice{Length: s.Length, Capacity: s.Capacity, Ref: s.Ref.materialize()}
}

// TypeKind identifies a decoded TypeSpec, separately from a data Value's Kind.
type TypeKind Uint

const (
	TypeIDKind      = TypeKind(typeSpecTypeID)
	TypePointerKind = TypeKind(typeSpecPointer)
	TypeArrayKind   = TypeKind(typeSpecArray)
	TypeSliceKind   = TypeKind(typeSpecSlice)
	TypeMapKind     = TypeKind(typeSpecMap)
	TypeNilKind     = TypeKind(typeSpecNil)
)

// TypeView reads decoded type metadata in either temporary or owned storage.
// It contains real Go pointers/interfaces, never an assumed runtime ABI.
type TypeView struct {
	frame *valueFrame
	index int
	owned TypeSpec
}

// Kind returns the type-specification kind. The view must be valid.
func (v TypeView) Kind() TypeKind {
	if v.frame != nil {
		return TypeKind(v.frame.node(v.index).kind)
	}
	switch v.owned.(type) {
	case TypeID:
		return TypeIDKind
	case *TypeSpecPointer:
		return TypePointerKind
	case *TypeSpecArray:
		return TypeArrayKind
	case *TypeSpecSlice:
		return TypeSliceKind
	case *TypeSpecMap:
		return TypeMapKind
	case TypeSpecNil:
		return TypeNilKind
	default:
		panic("invalid owned type specification")
	}
}

// ID returns a concrete type ID.
func (v TypeView) ID() TypeID {
	if v.Kind() != TypeIDKind {
		panic("type specification is not a concrete ID")
	}
	if v.frame != nil {
		return TypeID(v.frame.node(v.index).a)
	}
	return v.owned.(TypeID)
}

// Count returns an array's full-width encoded element count.
func (v TypeView) Count() Uint {
	if v.Kind() != TypeArrayKind {
		panic("type specification is not an array")
	}
	if v.frame != nil {
		return Uint(v.frame.node(v.index).a)
	}
	return v.owned.(*TypeSpecArray).Count
}

// Element returns a pointer, array or slice element type.
func (v TypeView) Element() TypeView {
	kind := v.Kind()
	switch kind {
	case TypePointerKind, TypeArrayKind, TypeSliceKind:
	default:
		panic("type specification has no element")
	}
	if v.frame != nil {
		node := v.frame.node(v.index)
		index := node.a
		if kind == TypeArrayKind {
			index = node.b
		}
		return TypeView{frame: v.frame, index: int(index)}
	}
	switch typ := v.owned.(type) {
	case *TypeSpecPointer:
		return TypeView{owned: typ.Type}
	case *TypeSpecArray:
		return TypeView{owned: typ.Type}
	case *TypeSpecSlice:
		return TypeView{owned: typ.Type}
	default:
		panic("type specification has no element")
	}
}

// Map returns the key and value types of a map.
func (v TypeView) Map() (TypeView, TypeView) {
	if v.Kind() != TypeMapKind {
		panic("type specification is not a map")
	}
	if v.frame != nil {
		node := v.frame.node(v.index)
		return TypeView{frame: v.frame, index: int(node.a)}, TypeView{frame: v.frame, index: int(node.b)}
	}
	typ := v.owned.(*TypeSpecMap)
	return TypeView{owned: typ.Key}, TypeView{owned: typ.Value}
}

func (v TypeView) materialize() TypeSpec {
	if v.frame == nil {
		return v.owned
	}
	switch v.Kind() {
	case TypeIDKind:
		id := v.ID()
		return id // Escapes: boxing the owned type ID.
	case TypePointerKind:
		typ := v.Element().materialize()
		return &TypeSpecPointer{Type: typ} // Escapes: owned type metadata.
	case TypeArrayKind:
		count := v.Count()
		typ := v.Element().materialize()
		return &TypeSpecArray{Count: count, Type: typ} // Escapes: owned type metadata.
	case TypeSliceKind:
		typ := v.Element().materialize()
		return &TypeSpecSlice{Type: typ} // Escapes: owned type metadata.
	case TypeMapKind:
		key, value := v.Map()
		keyType, valueType := key.materialize(), value.materialize()
		return &TypeSpecMap{Key: keyType, Value: valueType} // Escapes: owned type metadata.
	case TypeNilKind:
		return TypeSpecNil{}
	default:
		panic("invalid parsed type specification")
	}
}

// InterfaceView returns the dynamic type and value without allocating a
// legacy TypeSpec tree. Nil type delegation remains distinct from nil storage.
func (v Value) InterfaceView() (TypeView, Value) {
	v.require(KindInterface)
	if frame := v.frame(); frame != nil {
		node := frame.node(int(v.bits))
		return TypeView{frame: frame, index: int(node.a)}, frameValue(frame, int(node.b))
	}
	typ, value := v.Interface()
	return TypeView{owned: typ}, value
}
