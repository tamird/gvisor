// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
	"io"
	"sort"
	"strconv"
	"strings"
)

// restoreSet binds normal Load fields independently of capture records. Its
// static functions specialize graph owners before deferred data is restored;
// they never capture a field address or replace an existing graph owner.
type restoreSet struct {
	pkg        *types.Package
	scope      *types.Scope
	loader     *types.Interface
	names      map[string]struct{}
	paths      map[*types.Package]string
	qualifiers map[*types.Package]string
	imports    map[string]string
	shapes     []*restoreShape
	registered map[string]*restoreShape
	fields     map[string]map[string]restoreEncoding
	receiver   string
	decoder    string
	target     string
	value      string
	namespace  string
}

type restoreShape struct {
	typ          types.Type
	spelling     string
	typed        string
	erased       string
	array        string
	pointerArray string
	encoding     restoreEncoding
}

type restoreEncoding struct {
	helper  string
	object  *restoreShape
	key     string
	element string
}

func checkedRestores(file *ast.File, info *types.Info, pkg *types.Package, model *groupModel, sources map[string]*ast.File, names map[string]struct{}) (*restoreSet, error) {
	r := &restoreSet{
		pkg: pkg, scope: info.Scopes[file], names: names,
		paths: make(map[*types.Package]string), qualifiers: make(map[*types.Package]string), imports: make(map[string]string),
		registered: make(map[string]*restoreShape), fields: make(map[string]map[string]restoreEncoding),
		receiver: unusedName("stateReceiver", names), decoder: unusedName("stateDecoder", names),
		target: unusedName("stateTarget", names), value: unusedName("stateValue", names),
	}
	if len(model.types) == 0 {
		return r, nil
	}
	// Suffix groups emit separate files into the same package. The first
	// declared type gives their helper declarations disjoint namespaces.
	ownerName := model.types[0].spec.Name.Name
	r.namespace = fmt.Sprintf("%d_%s", len(ownerName), ownerName)
	// An archive proves package identity, not that it is a direct dependency.
	// Only an actual checked source import authorizes a newly emitted import.
	var sourceNames []string
	for name := range sources {
		sourceNames = append(sourceNames, name)
	}
	sort.Strings(sourceNames)
	for _, name := range sourceNames {
		for _, spec := range sources[name].Imports {
			var object types.Object
			if spec.Name == nil {
				object = info.Implicits[spec]
			} else {
				object = info.Defs[spec.Name]
			}
			imported, ok := object.(*types.PkgName)
			if !ok {
				continue
			}
			path, err := strconv.Unquote(spec.Path.Value)
			if err != nil {
				return nil, err
			}
			if _, exists := r.paths[imported.Imported()]; !exists {
				r.paths[imported.Imported()] = path
			}
		}
	}
	statePackage := pkg
	for _, name := range r.scope.Names() {
		if imported, ok := r.scope.Lookup(name).(*types.PkgName); ok {
			r.qualifiers[imported.Imported()] = name
		}
	}
	for _, spec := range file.Imports {
		path, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			return nil, err
		}
		if path == statePkg {
			statePackage = info.Implicits[spec].(*types.PkgName).Imported()
		}
	}
	loader := statePackage.Scope().Lookup("SaverLoader")
	if loader == nil {
		return nil, fmt.Errorf("no checked state.SaverLoader interface")
	}
	r.loader = loader.Type().Underlying().(*types.Interface)
	for _, typ := range model.types {
		name := typ.spec.Name.Name
		object, ok := pkg.Scope().Lookup(name).(*types.TypeName)
		if !ok {
			return nil, fmt.Errorf("no checked restore type %s", name)
		}
		owner := object.Type()
		if _, structural := owner.Underlying().(*types.Struct); structural && types.Implements(types.NewPointer(owner), r.loader) {
			if shape := r.shape(owner); shape != nil {
				shape.array = unusedName(shape.erased+"Array", r.names)
				shape.pointerArray = unusedName(shape.erased+"PointerArray", r.names)
				r.registered[name] = shape
			}
		}
		// Handwritten and forwarding loaders own their own fields. Their
		// exact typed graph callback still invokes the existing StateLoad.
		if !typ.save {
			continue
		}
		if _, structural := typ.spec.Type.(*ast.StructType); !structural {
			continue
		}
		fields := make(map[string]restoreEncoding)
		for _, field := range typ.fields {
			if field.kind != fieldNormal && field.kind != fieldWait {
				continue
			}
			member, _, _ := types.LookupFieldOrMethod(types.NewPointer(owner), true, pkg, field.name)
			if member == nil {
				return nil, fmt.Errorf("%s has no checked restore field %s", name, field.name)
			}
			fields[field.name] = r.encoding(member.Type())
		}
		r.fields[name] = fields
	}
	return r, nil
}

// nameable checks the spelling before removing aliases for identity. In
// particular, an exported named map does not require spelling its private
// underlying components; inference supplies those at its operation call.
func (r *restoreSet) nameable(t types.Type) bool {
	objectNameable := func(object *types.TypeName, args *types.TypeList) bool {
		if object.Pkg() == nil {
			_, found := r.scope.LookupParent(object.Name(), token.NoPos)
			if found != types.Universe.Lookup(object.Name()) {
				return false
			}
		} else if object.Pkg() == r.pkg {
			_, found := r.scope.LookupParent(object.Name(), token.NoPos)
			if found != object {
				return false
			}
		} else if !object.Exported() || r.paths[object.Pkg()] == "" {
			return false
		}
		for i := 0; i < args.Len(); i++ {
			if !r.nameable(args.At(i)) {
				return false
			}
		}
		return true
	}
	switch t := t.(type) {
	case *types.Alias:
		return objectNameable(t.Obj(), t.TypeArgs())
	case *types.Named:
		return objectNameable(t.Obj(), t.TypeArgs())
	case *types.Basic:
		if t.Kind() == types.UnsafePointer {
			// Bare unsafe.Pointer would require an unsafe generated file.
			// Named or aliased spellings are checked above instead.
			return false
		}
		builtin := types.Universe.Lookup(t.Name())
		_, found := r.scope.LookupParent(t.Name(), token.NoPos)
		return builtin != nil && found == builtin
	case *types.Pointer:
		return r.nameable(t.Elem())
	case *types.Slice:
		return r.nameable(t.Elem())
	case *types.Array:
		return r.nameable(t.Elem())
	case *types.Map:
		return r.nameable(t.Key()) && r.nameable(t.Elem())
	case *types.Interface:
		if t.NumMethods() != 0 || !t.IsMethodSet() {
			return false
		}
		for i := 0; i < t.NumEmbeddeds(); i++ {
			if !r.nameable(t.EmbeddedType(i)) {
				return false
			}
		}
		return true
	default:
		return false
	}
}

func (r *restoreSet) qualify(pkg *types.Package) string {
	if pkg == r.pkg {
		return ""
	}
	if name := r.qualifiers[pkg]; name != "" {
		return name
	}
	name := unusedName(pkg.Name(), r.names)
	r.qualifiers[pkg] = name
	r.imports[r.paths[pkg]] = name
	return name
}

// shape installs identity only after finding a legal spelling. Recursive
// types can then refer to the same declaration while its body is assembled.
func (r *restoreSet) shape(t types.Type) *restoreShape {
	if !r.nameable(t) {
		return nil
	}
	for _, shape := range r.shapes {
		if types.Identical(shape.typ, t) {
			return shape
		}
	}
	shape := &restoreShape{
		typ: t, spelling: types.TypeString(t, r.qualify),
		typed:  unusedName(fmt.Sprintf("restoreState%s_%d", r.namespace, len(r.shapes)), r.names),
		erased: unusedName(fmt.Sprintf("restoreStateObject%s_%d", r.namespace, len(r.shapes)), r.names),
	}
	r.shapes = append(r.shapes, shape)
	shape.encoding = r.encoding(t)
	return shape
}

func (r *restoreSet) callback(t types.Type) string {
	switch t.Underlying().(type) {
	case *types.Basic, *types.Struct, *types.Interface:
		return r.simpleHelper(t)
	}
	if shape := r.shape(t); shape != nil {
		return shape.typed
	}
	return "RestoreOpaque"
}

func (r *restoreSet) simpleHelper(t types.Type) string {
	switch t := t.Underlying().(type) {
	case *types.Basic:
		switch t.Kind() {
		case types.Bool:
			return "RestoreBool"
		case types.Int, types.Int8, types.Int16, types.Int32, types.Int64:
			return "RestoreInt"
		case types.Uint, types.Uint8, types.Uint16, types.Uint32, types.Uint64, types.Uintptr:
			return "RestoreUint"
		case types.Float32, types.Float64:
			return "RestoreFloat"
		case types.Complex64, types.Complex128:
			return "RestoreComplex"
		case types.String:
			return "RestoreString"
		}
	}
	if _, ok := t.Underlying().(*types.Struct); ok && types.Implements(types.NewPointer(t), r.loader) {
		return "RestoreStruct"
	}
	return "RestoreOpaque"
}

func (r *restoreSet) encoding(t types.Type) restoreEncoding {
	switch underlying := t.Underlying().(type) {
	case *types.Pointer:
		return restoreEncoding{helper: "RestorePointer", object: r.shape(underlying.Elem())}
	case *types.Slice:
		return restoreEncoding{helper: "RestoreSlice", element: r.callback(underlying.Elem())}
	case *types.Array:
		return restoreEncoding{helper: "RestoreArray", element: r.callback(underlying.Elem())}
	case *types.Map:
		return restoreEncoding{helper: "RestoreMap", object: r.shape(t), key: r.callback(underlying.Key()), element: r.callback(underlying.Elem())}
	default:
		return restoreEncoding{helper: r.simpleHelper(t)}
	}
}

func (e restoreEncoding) emit(w io.Writer, prefix, decoder, target, value string) {
	args := []string{decoder, target}
	if e.helper == "RestoreArray" {
		args = append(args, "("+target+")[:]")
	}
	args = append(args, value)
	if e.helper == "RestorePointer" || e.helper == "RestoreMap" {
		object := "nil"
		if e.object != nil {
			object = e.object.erased
		}
		args = append(args, object)
	}
	for _, callback := range []string{e.key, e.element} {
		if callback != "" {
			if strings.HasPrefix(callback, "Restore") {
				callback = prefix + callback
			}
			args = append(args, callback)
		}
	}
	fmt.Fprintf(w, " %s%s(%s)\n", prefix, e.helper, strings.Join(args, ", "))
}

func (r *restoreSet) declare(w io.Writer, prefix, wireAlias string) {
	for _, shape := range r.shapes {
		fmt.Fprintf(w, "func %s(%s %sDecoder, %s *%s, %s %s.Value) {\n", shape.typed, r.decoder, prefix, r.target, shape.spelling, r.value, wireAlias)
		shape.encoding.emit(w, prefix, r.decoder, r.target, r.value)
		fmt.Fprint(w, "}\n\n")
		fmt.Fprintf(w, "func %s(%s %sDecoder, %s interface{}, %s %s.Value) {\n", shape.erased, r.decoder, prefix, r.target, r.value, wireAlias)
		fmt.Fprintf(w, " %s(%s, %s.(*%s), %s)\n}\n\n", shape.typed, r.decoder, r.target, shape.spelling, r.value)
		if shape.array != "" {
			fmt.Fprintf(w, "func %s(%s %sDecoder, %s interface{}, %s %s.Value) {\n", shape.array, r.decoder, prefix, r.target, r.value, wireAlias)
			fmt.Fprintf(w, " %sRestoreRuntimeArray(%s, %s, %s, %s)\n}\n\n", prefix, r.decoder, r.target, r.value, shape.typed)
			fmt.Fprintf(w, "func %s(%s %sDecoder, %s interface{}, %s %s.Value) {\n", shape.pointerArray, r.decoder, prefix, r.target, r.value, wireAlias)
			fmt.Fprintf(w, " %sRestoreRuntimePointerArray[%s](%s, %s, %s, %s)\n}\n\n", prefix, shape.spelling, r.decoder, r.target, r.value, shape.erased)
		}
	}
}
