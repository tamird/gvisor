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
	"go/parser"
	"path"
	"sort"
	"strconv"
	"strings"
)

type importSpec struct {
	alias string
	path  string
}

func (i importSpec) name() string {
	if i.alias != "" {
		return i.alias
	}
	return path.Base(i.path)
}

// sourceImports records the spelling used in each input file. In particular,
// an explicit alias may be the last component of the path without being the
// package's default name, as in v2 "math/rand/v2".
func sourceImports(file *ast.File) []importSpec {
	var imports []importSpec
	for _, spec := range file.Imports {
		p, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			panic(err) // ParseFile checked the import literal.
		}
		i := importSpec{path: p}
		if spec.Name != nil {
			i.alias = spec.Name.Name
		}
		imports = append(imports, i)
	}
	return imports
}

type generatedImports struct {
	extra    []importSpec
	siblings []importSpec
	used     map[string]importSpec
}

func (g *generatedImports) use(i importSpec) {
	name := i.name()
	if prior, ok := g.used[name]; ok && prior.path != i.path {
		fail("generated code uses %s for both %q and %q", name, prior.path, i.path)
	}
	g.used[name] = i
}

// A conversion tag may name a type imported only in a sibling's save/load
// methods, or supplied explicitly in the rule's imports. An identtype's
// selector belongs to the file that declares it. Prefer that file's binding.
func (g *generatedImports) useType(expr ast.Expr, local []importSpec) {
	for n := range ast.Preorder(expr) {
		sel, ok := n.(*ast.SelectorExpr)
		if !ok {
			continue
		}
		id, ok := sel.X.(*ast.Ident)
		if !ok {
			continue
		}
		found := false
		for _, group := range [][]importSpec{local, g.extra, g.siblings} {
			for _, i := range group {
				if i.name() == id.Name {
					g.use(i)
					found = true
				}
			}
			if found {
				break
			}
		}
		if !found {
			fail("no import for %s in generated type; declare its alias in a stateify input", id.Name)
		}
	}
}

func (g *generatedImports) useTag(typ string, local []importSpec) {
	expr, err := parser.ParseExpr(typ)
	if err != nil {
		fail("invalid state tag type %q: %v", typ, err)
	}
	g.useType(expr, local)
}

func (g *generatedImports) write(w *bytes.Buffer) {
	if len(g.used) == 0 {
		return
	}
	// Keep standard library imports in their own group.
	var std, other []string
	for _, i := range g.used {
		s := strconv.Quote(i.path)
		if i.alias != "" {
			s = i.alias + " " + s
		}
		first, _, _ := strings.Cut(i.path, "/")
		if strings.Contains(first, ".") {
			other = append(other, s)
		} else {
			std = append(std, s)
		}
	}
	sort.Strings(std)
	sort.Strings(other)
	fmt.Fprintln(w, "import (")
	for _, s := range std {
		fmt.Fprintln(w, s)
	}
	if len(std) != 0 && len(other) != 0 {
		fmt.Fprintln(w)
	}
	for _, s := range other {
		fmt.Fprintln(w, s)
	}
	fmt.Fprintln(w, ")")
}
