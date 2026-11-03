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
	"log"
	"path"
	"strconv"
	"strings"
)

type packageImport struct {
	name string // Empty for unaliased imports.
	path string
}

type importSet map[string]packageImport

// An unaliased versioned import can use the last path component or the
// preceding package name (e.g. math/rand/v2 is rand).
// Other packages whose declared name differs from their path can use an
// explicit alias in the source.
func (imp packageImport) matches(name string) bool {
	if imp.name != "" {
		return imp.name == name
	}
	base := path.Base(imp.path)
	if base == name {
		return true
	}
	if version, ok := strings.CutPrefix(base, "v"); ok {
		if n, err := strconv.ParseUint(version, 10, 64); err == nil && n >= 2 && version == strconv.FormatUint(n, 10) {
			return path.Base(path.Dir(imp.path)) == name
		}
	}
	return false
}

func (used importSet) add(name string, imp packageImport) {
	if prev, ok := used[name]; ok {
		if prev.path != imp.path {
			log.Fatalf("generated code uses %q for both %q and %q", name, prev.path, imp.path)
		}
		if prev.name != "" {
			return
		}
	}
	used[name] = imp
}

// requireType records package selectors in a conversion type, including
// selectors nested in map, array, and instantiated generic types.
func (used importSet) requireType(typ string, file *ast.File, hooks ...*ast.File) {
	expr, err := parser.ParseExpr(typ)
	if err != nil {
		log.Fatalf("invalid state conversion type %q: %v", typ, err)
	}
	for n := range ast.Preorder(expr) {
		if sel, ok := n.(*ast.SelectorExpr); ok {
			if id, ok := sel.X.(*ast.Ident); ok {
				used.require(id.Name, file, hooks...)
			}
		}
	}
}

// require records the import used by an emitted type. Honor explicit bindings
// for generated code first. Otherwise, a conversion tag may use the spelling
// from its save or load method in another input file.
func (used importSet) require(name string, file *ast.File, hooks ...*ast.File) {
	found := false
	for entry := range strings.SplitSeq(*imports, ",") {
		if entry == "" {
			continue
		}
		alias, p, named := strings.Cut(entry, "=")
		imp := packageImport{path: entry}
		if named {
			imp = packageImport{name: alias, path: p}
		}
		if imp.matches(name) {
			used.add(name, imp)
			found = true
		}
	}
	if found {
		return
	}
	from := func(f *ast.File) {
		if f == nil {
			return
		}
		for _, spec := range f.Imports {
			p, err := strconv.Unquote(spec.Path.Value)
			if err != nil {
				log.Fatal(err)
			}
			imp := packageImport{path: p}
			if spec.Name != nil {
				imp.name = spec.Name.Name
			}
			if imp.matches(name) {
				used.add(name, imp)
				found = true
			}
		}
	}
	from(file)
	if !found {
		for _, f := range hooks {
			from(f)
		}
	}
	if found {
		return
	}
	log.Fatalf("no import for %q in generated type; declare its alias in the input source or pass -imports=%s=path", name, name)
}

func (used importSet) writeTo(out *bytes.Buffer) {
	if len(used) == 0 {
		return
	}
	// go/format sorts each group.
	var standard, other bytes.Buffer
	for _, imp := range used {
		group := &standard
		first, _, _ := strings.Cut(imp.path, "/")
		if strings.Contains(first, ".") {
			group = &other
		}
		fmt.Fprintf(group, "%s %q\n", imp.name, imp.path)
	}
	out.WriteString("import (\n")
	out.Write(standard.Bytes())
	if standard.Len() != 0 && other.Len() != 0 {
		out.WriteByte('\n')
	}
	out.Write(other.Bytes())
	out.WriteString(")\n\n")
}
