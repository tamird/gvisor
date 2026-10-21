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

package main

import (
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
	"io"
	"strings"
)

// decoderEmitter projects zero values to preserve static types, including
// unexported element types of imported named containers. No factory reads a
// sample or retains its address. Named structs supply their own lazy factories.
type decoderEmitter struct {
	out       io.Writer
	prefix    string
	parameter string
	used      map[string]bool
	seen      map[types.Type]bool
	pkg       *types.Package
}

func (e *decoderEmitter) name(base string) string {
	name := base
	for n := 1; e.used[name]; n++ {
		name = fmt.Sprintf("%s%d", base, n)
	}
	e.used[name] = true
	return name
}

func (e *decoderEmitter) emit(t types.Type, expression string) {
	t = types.Unalias(t)
	if e.seen[t] {
		return
	}
	e.seen[t] = true
	helper := "AddValue"
	switch underlying := t.Underlying().(type) {
	case *types.Basic:
		switch {
		case underlying.Info()&types.IsBoolean != 0:
			helper = "AddBool"
		case underlying.Info()&types.IsUnsigned != 0:
			helper = "AddUint"
		case underlying.Info()&types.IsInteger != 0:
			helper = "AddInt"
		case underlying.Info()&types.IsFloat != 0:
			helper = "AddFloat"
		case underlying.Info()&types.IsComplex != 0:
			helper = "AddComplex"
		case underlying.Info()&types.IsString != 0:
			helper = "AddString"
		}
	case *types.Map:
		helper = "AddMap"
	case *types.Slice:
		helper = "AddSlice"
	case *types.Array:
		fmt.Fprintf(e.out, " %sAddArray(%s, &%s, %s[:])\n", e.prefix, e.parameter, expression, expression)
		e.child(underlying.Elem(), "SliceTypes", expression+"[:]")
		return
	case *types.Pointer:
		helper = "AddPointer"
	}
	fmt.Fprintf(e.out, " %s%s(%s, &%s)\n", e.prefix, helper, e.parameter, expression)
	switch underlying := t.Underlying().(type) {
	case *types.Map:
		key, value := e.name("key"), e.name("value")
		fmt.Fprintf(e.out, " %s, %s := %sMapTypes(%s)\n", key, value, e.prefix, expression)
		// A repeated child still has to be syntactically used in the projection.
		fmt.Fprintf(e.out, " _, _ = %s, %s\n", key, value)
		e.emit(underlying.Key(), key)
		e.emit(underlying.Elem(), value)
	case *types.Slice:
		e.child(underlying.Elem(), "SliceTypes", expression)
	case *types.Pointer:
		e.child(underlying.Elem(), "PointerTypes", expression)
	case *types.Struct:
		if _, named := t.(*types.Named); named {
			return
		}
		for i := 0; i < underlying.NumFields(); i++ {
			field := underlying.Field(i)
			if field.Name() == "_" || !field.Exported() && field.Pkg() != e.pkg {
				continue
			}
			e.emit(field.Type(), expression+"."+field.Name())
		}
	}
}

func (e *decoderEmitter) child(t types.Type, helper, expression string) {
	if e.seen[types.Unalias(t)] {
		return
	}
	name := e.name("element")
	fmt.Fprintf(e.out, " %s := %s%s(%s)\n", name, e.prefix, helper, expression)
	e.emit(t, name)
}

func emitDecoderTypes(out io.Writer, spec *ast.TypeSpec, prefix string, identical bool, fset *token.FileSet, canonical *ast.File, pkg *types.Package, info *types.Info, generatedTypes map[string]bool) error {
	object, ok := info.Defs[spec.Name].(*types.TypeName)
	if !ok {
		return fmt.Errorf("no checked type for %s", spec.Name)
	}
	used := make(map[string]bool)
	for _, name := range pkg.Scope().Names() {
		used[name] = true
	}
	for _, imported := range pkg.Imports() {
		used[imported.Name()] = true
	}
	for _, name := range types.Universe.Names() {
		used[name] = true
	}
	used[strings.TrimSuffix(prefix, ".")] = true
	e := &decoderEmitter{out: out, prefix: prefix, used: used, seen: make(map[types.Type]bool), pkg: pkg}
	e.parameter = e.name("decoders")
	sample := e.name("sample")
	fmt.Fprintf(out, "func (*%s) StateDecodeTypes(%s *%sDecoderTypes) {\n", spec.Name, e.parameter, prefix)
	fmt.Fprintf(out, " var %s %s\n", sample, spec.Name)
	e.emit(object.Type(), sample)
	if identical {
		// Identical-type wrappers bypass Source.Load when they delegate to
		// the original StateLoad, so forward its lazy factory here as well.
		original := types.Unalias(info.TypeOf(spec.Type))
		method, _, _ := types.LookupFieldOrMethod(types.NewPointer(original), true, pkg, "StateDecodeTypes")
		generated := false
		if named, ok := original.(*types.Named); ok {
			generated = named.Obj().Pkg() == pkg && generatedTypes[named.Obj().Name()]
		}
		if method != nil || generated {
			fmt.Fprintf(out, " (*%s)(&%s).StateDecodeTypes(%s)\n", types.ExprString(spec.Type), sample, e.parameter)
		}
	}
	if structure, ok := spec.Type.(*ast.StructType); ok {
		var result error
		field := func(name string) {
			member, _, _ := types.LookupFieldOrMethod(object.Type(), true, pkg, name)
			if member == nil {
				result = fmt.Errorf("no checked field %s.%s", spec.Name, name)
				return
			}
			e.emit(member.Type(), sample+"."+name)
		}
		scanFields(structure, scanFunctions{normal: field, wait: field, value: func(name, spelling string) {
			value, err := types.Eval(fset, pkg, canonical.Name.End(), spelling)
			if err != nil {
				result = fmt.Errorf("custom value type %s.%s: %w", spec.Name, name, err)
				return
			}
			if !value.IsType() {
				result = fmt.Errorf("custom value %s.%s requires a type, got %s", spec.Name, name, spelling)
				return
			}
			if !e.seen[types.Unalias(value.Type)] {
				local := e.name("value")
				fmt.Fprintf(out, " var %s %s\n", local, spelling)
				e.emit(value.Type, local)
			}
		}})
		if result != nil {
			return result
		}
	}
	fmt.Fprint(out, "}\n\n")
	return nil
}
