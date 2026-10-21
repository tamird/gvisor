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
	"bytes"
	"fmt"
	"go/ast"
	"go/types"
	"io"
	"strings"
)

// loadEmitter preserves static field types at the existing StateLoad boundary.
// Projections produce zero child values, including private imported types;
// operations never retain these samples. Structs and interfaces enter the
// shared value loader, and structs' StateLoad methods select their own fields.
type loadEmitter struct {
	out          io.Writer
	pkg          *types.Package
	info         *types.Info
	used         map[string]bool
	declarations bytes.Buffer
}

func newLoadEmitter(out io.Writer, pkg *types.Package, info *types.Info) *loadEmitter {
	used := make(map[string]bool)
	for _, name := range pkg.Scope().Names() {
		used[name] = true
	}
	for _, imported := range pkg.Imports() {
		used[imported.Name()] = true
	}
	return &loadEmitter{out: out, pkg: pkg, info: info, used: used}
}

func (e *loadEmitter) name(base string) string {
	name := base
	for n := 1; e.used[name]; n++ {
		name = fmt.Sprintf("%s%d", base, n)
	}
	e.used[name] = true
	return name
}

func scalarOperation(t types.Type) string {
	if basic, ok := t.Underlying().(*types.Basic); ok {
		switch {
		case basic.Info()&types.IsBoolean != 0:
			return "BoolOps"
		case basic.Info()&types.IsUnsigned != 0:
			return "UintOps"
		case basic.Info()&types.IsInteger != 0:
			return "IntOps"
		case basic.Info()&types.IsFloat != 0:
			return "FloatOps"
		case basic.Info()&types.IsComplex != 0:
			return "ComplexOps"
		case basic.Info()&types.IsString != 0:
			return "StringOps"
		}
	}
	return "ValueOps"
}

// containerChildren stops at the common struct/interface loader. Only a cycle
// consisting entirely of containers needs a named operation to terminate its
// compile-time expansion.
func containerChildren(t types.Type) []types.Type {
	switch t := t.Underlying().(type) {
	case *types.Map:
		return []types.Type{t.Key(), t.Elem()}
	case *types.Slice:
		return []types.Type{t.Elem()}
	case *types.Array:
		return []types.Type{t.Elem()}
	case *types.Pointer:
		return []types.Type{t.Elem()}
	default:
		return nil
	}
}

func recursiveContainer(t types.Type, active map[types.Type]bool) bool {
	t = types.Unalias(t)
	if active[t] {
		return true
	}
	active[t] = true
	defer delete(active, t)
	for _, child := range containerChildren(t) {
		if recursiveContainer(child, active) {
			return true
		}
	}
	return false
}

func (e *loadEmitter) field(spec *ast.TypeSpec, field, target string, slot int, wait bool, prefix string) error {
	object, ok := e.info.Defs[spec.Name].(*types.TypeName)
	if !ok {
		return fmt.Errorf("no checked type for %s", spec.Name)
	}
	member, _, _ := types.LookupFieldOrMethod(object.Type(), true, e.pkg, field)
	if member == nil {
		return fmt.Errorf("no checked field %s.%s", spec.Name, field)
	}
	e.used[strings.Split(target, ".")[0]] = true
	e.used[strings.TrimSuffix(prefix, ".")] = true
	fmt.Fprintln(e.out, " {")
	var operation string
	if recursiveContainer(member.Type(), make(map[types.Type]bool)) {
		factory := e.recursiveOperation(member.Type(), "stateLoad"+spec.Name.Name+field, prefix)
		operation = factory + "(&" + target + ")"
	} else {
		operation = e.operation(member.Type(), target, prefix)
	}
	helper := "LoadField"
	if wait {
		helper = "LoadWaitField"
	}
	fmt.Fprintf(e.out, " %s%s(stateSourceObject, %d, &%s, %s)\n }\n", prefix, helper, slot, target, operation)
	return nil
}

func (e *loadEmitter) operation(t types.Type, target, prefix string) string {
	child := func(t types.Type, helper, expression string) string {
		name := e.name("stateElement")
		fmt.Fprintf(e.out, " %s := %s%s(%s)\n", name, prefix, helper, expression)
		return e.operation(t, name, prefix)
	}
	switch underlying := t.Underlying().(type) {
	case *types.Map:
		key, value := e.name("stateKey"), e.name("stateValue")
		fmt.Fprintf(e.out, " %s, %s := %sMapTypes(%s)\n", key, value, prefix, target)
		return fmt.Sprintf("%sMapOps(&%s, %s, %s)", prefix, target, e.operation(underlying.Key(), key, prefix), e.operation(underlying.Elem(), value, prefix))
	case *types.Slice:
		return fmt.Sprintf("%sSliceOps(&%s, %s)", prefix, target, child(underlying.Elem(), "SliceTypes", target))
	case *types.Pointer:
		return fmt.Sprintf("%sPointerOps(&%s, %s)", prefix, target, child(underlying.Elem(), "PointerTypes", target))
	case *types.Array:
		return fmt.Sprintf("%sArrayOps(&%s, %s[:], %s)", prefix, target, target, child(underlying.Elem(), "SliceTypes", target+"[:]"))
	default:
		return fmt.Sprintf("%s%s(&%s)", prefix, scalarOperation(t), target)
	}
}

// recursiveOperation emits stateless type adapters, never a traversal. Generic
// shape constraints preserve private imported children and terminate recursive
// container instantiation without spelling those children's names. All nodes
// share the same finite parameter list; their methods delegate to runtime ops.
func (e *loadEmitter) recursiveOperation(root types.Type, base, prefix string) string {
	indices := make(map[types.Type]int)
	var nodes []types.Type
	var visit func(types.Type)
	visit = func(t types.Type) {
		t = types.Unalias(t)
		if _, exists := indices[t]; exists {
			return
		}
		indices[t] = len(nodes)
		nodes = append(nodes, t)
		for _, child := range containerChildren(t) {
			visit(child)
		}
	}
	visit(root)
	parameter := func(t types.Type) string { return fmt.Sprintf("T%d", indices[types.Unalias(t)]) }
	keys := make(map[types.Type]bool)
	for _, t := range nodes {
		if m, ok := t.Underlying().(*types.Map); ok {
			keys[types.Unalias(m.Key())] = true
		}
	}
	var parameters, constraints []string
	for _, t := range nodes {
		constraint := "any"
		switch underlying := t.Underlying().(type) {
		case *types.Basic:
			if scalarOperation(t) != "ValueOps" {
				constraint = "~" + underlying.Name()
			}
		case *types.Map:
			constraint = "~map[" + parameter(underlying.Key()) + "]" + parameter(underlying.Elem())
		case *types.Slice:
			constraint = "~[]" + parameter(underlying.Elem())
		case *types.Pointer:
			constraint = "~*" + parameter(underlying.Elem())
		case *types.Array:
			constraint = fmt.Sprintf("~[%d]%s", underlying.Len(), parameter(underlying.Elem()))
		}
		if keys[t] {
			constraint = "interface { comparable; " + constraint + " }"
		}
		parameters = append(parameters, parameter(t))
		constraints = append(constraints, parameter(t)+" "+constraint)
	}
	arguments := "[" + strings.Join(parameters, ", ") + "]"
	declaration := "[" + strings.Join(constraints, ", ") + "]"
	factory := e.name(base)
	names := make([]string, len(nodes))
	for i, t := range nodes {
		if len(containerChildren(t)) != 0 {
			names[i] = e.name(fmt.Sprintf("%sOp%d", factory, i))
		}
	}
	out := &e.declarations
	fmt.Fprintf(out, "func %s%s(_ *T0) %s%s { return %s%s{} }\n", factory, declaration, names[0], arguments, names[0], arguments)
	for i, t := range nodes {
		if names[i] == "" {
			continue
		}
		fmt.Fprintf(out, "type %s%s struct{}\n", names[i], declaration)
		fmt.Fprintf(out, "func (%s%s) Restore(d %sDecoder, target *T%d) {\n", names[i], arguments, prefix, i)
		child := func(t types.Type) string {
			index := indices[types.Unalias(t)]
			if names[index] != "" {
				return names[index] + arguments + "{}"
			}
			// A separate zero local supplies an otherwise unnameable child.
			name := e.name("stateChild")
			fmt.Fprintf(out, " var %s T%d\n", name, index)
			return fmt.Sprintf("%s%s(&%s)", prefix, scalarOperation(t), name)
		}
		var operation string
		switch t := t.Underlying().(type) {
		case *types.Map:
			operation = fmt.Sprintf("%sMapOps(target, %s, %s)", prefix, child(t.Key()), child(t.Elem()))
		case *types.Slice:
			operation = fmt.Sprintf("%sSliceOps(target, %s)", prefix, child(t.Elem()))
		case *types.Pointer:
			operation = fmt.Sprintf("%sPointerOps(target, %s)", prefix, child(t.Elem()))
		case *types.Array:
			operation = fmt.Sprintf("%sArrayOps(target, (*target)[:], %s)", prefix, child(t.Elem()))
		}
		fmt.Fprintf(out, " %s.Restore(d, target)\n}\n", operation)
	}
	return factory
}
