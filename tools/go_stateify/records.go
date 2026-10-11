// Copyright 2018 The gVisor Authors.
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
	"encoding/json"
	"fmt"
	"go/ast"
	"go/build"
	"go/constant"
	"go/parser"
	"go/token"
	"go/types"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/tools/go/gcexportdata"
)

// packageConfig describes only declared inputs of the owning Bazel action.
// Groups preserve stateify's existing per-file-suffix hook visibility and
// outputs. Sources additionally includes independent generated Go files.
type packageConfig struct {
	Package      string
	ImportPath   string
	StatePackage string
	Imports      []string
	Sources      []string
	Groups       []sourceGroup
	Archives     map[string]compilerExport
	Stdlib       []string
	GOOS         string
	GOARCH       string
	Tags         []string
	GoVersion    string
}

// compilerExport separates source import spellings from compiler identity.
type compilerExport struct {
	File      string
	ImportMap string
}

type sourceGroup struct {
	Sources []string
	Output  string
}

type recordSet struct {
	wireAlias string
	types     map[string]*recordType
}

type recordType struct {
	name    string
	emitter string
	fields  []primitive
}

// primitive names concrete wire storage and its corresponding unboxed emitter.
// Object represents a dynamic or graph-bearing child, whose stable slot is
// owned by the same record. No owner structs or ignored fields are copied.
type primitive struct {
	storage string
	emit    string
}

func primitiveFor(t types.Type) primitive {
	if b, ok := t.Underlying().(*types.Basic); ok {
		switch b.Kind() {
		case types.Bool:
			return primitive{"Bool", "SaveBoolField"}
		case types.Int, types.Int8, types.Int16, types.Int32, types.Int64:
			return primitive{"Int", "SaveIntField"}
		case types.Uint, types.Uint8, types.Uint16, types.Uint32, types.Uint64, types.Uintptr:
			return primitive{"Uint", "SaveUintField"}
		case types.String:
			return primitive{"String", "SaveStringField"}
		case types.Float32:
			// Preserve reflect.Value.Float's promotion, including signaling NaNs.
			return primitive{"Float64", "SaveFloat32Field"}
		case types.Float64:
			return primitive{"Float64", "SaveFloat64Field"}
		case types.Complex64:
			return primitive{"Complex128", "SaveComplex64Field"}
		case types.Complex128:
			return primitive{"Complex128", "SaveComplex128Field"}
		}
	}
	return primitive{"Object", "Save"}
}

func (r *recordType) declare(w io.Writer, wireAlias string) {
	fmt.Fprintf(w, "type %s struct {\n", r.name)
	for i, f := range r.fields {
		fmt.Fprintf(w, " F%d %s.%s\n", i, wireAlias, f.storage)
	}
	fmt.Fprint(w, "}\n\n")
	fmt.Fprintf(w, "func %s(w *%s.Writer, value *%s) {\n", r.emitter, wireAlias, r.name)
	for i, f := range r.fields {
		fmt.Fprintf(w, " %s.%s(w, value.F%d)\n", wireAlias, f.emit, i)
	}
	fmt.Fprint(w, "}\n\n")
}

func (r *recordType) capture(w io.Writer, wireAlias, local string, slot int, expression string, value bool) {
	f := r.fields[slot]
	if f.storage != "Object" {
		fmt.Fprintf(w, " %s.F%d = %s.%s(%s)\n", local, slot, wireAlias, f.storage, expression)
	} else if value {
		fmt.Fprintf(w, " stateSinkObject.CaptureValue(%s, &%s.F%d)\n", expression, local, slot)
	} else {
		fmt.Fprintf(w, " stateSinkObject.Capture(&%s, &%s.F%d)\n", expression, local, slot)
	}
}

// exportImporter reads the exact compiler exports supplied by Bazel. It never
// resolves modules, invokes the Go command, or falls back to host packages.
type exportImporter struct {
	fset     *token.FileSet
	archives map[string]compilerExport
	packages map[string]*types.Package
}

func (i *exportImporter) Import(path string) (*types.Package, error) {
	if path == "unsafe" {
		return types.Unsafe, nil
	}
	export, ok := i.archives[path]
	if !ok {
		return nil, fmt.Errorf("no declared compiler export for %q", path)
	}
	if p := i.packages[export.ImportMap]; p != nil && p.Complete() {
		return p, nil
	}
	f, err := os.Open(export.File)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	reader, err := gcexportdata.NewReader(f)
	if err != nil {
		return nil, err
	}
	return gcexportdata.Read(reader, i.fset, i.packages, export.ImportMap)
}

func generateRecords(configPath string) error {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return err
	}
	var config packageConfig
	if err := json.Unmarshal(data, &config); err != nil {
		return err
	}
	*fullPkg, *statePkg, *imports = config.Package, config.StatePackage, strings.Join(config.Imports, ",")
	context := build.Default
	context.GOOS, context.GOARCH = config.GOOS, config.GOARCH
	context.CgoEnabled = false
	context.BuildTags = config.Tags
	fset := token.NewFileSet()
	selected := make(map[string]*ast.File)
	var files []*ast.File
	for _, name := range config.Sources {
		if filepath.Ext(name) != ".go" {
			continue
		}
		if _, ok := selected[name]; ok {
			continue
		}
		matches, err := context.MatchFile(filepath.Dir(name), filepath.Base(name))
		if err != nil {
			return err
		}
		if !matches {
			continue
		}
		parsed, err := parseFiles(fset, []string{name})
		if err != nil {
			return err
		}
		for _, imported := range parsed[0].Imports {
			if imported.Path.Value == `"C"` {
				return fmt.Errorf("typed stateify requires generated cgo Go inputs: %s", name)
			}
		}
		selected[name] = parsed[0]
		files = append(files, parsed[0])
	}
	// Render the existing complete methods in memory. The same original ASTs
	// and renderer produce final output after types are known; this text is
	// parsed only as input to the Go type checker, never rewritten.
	canonical := make([]*ast.File, len(config.Groups))
	groupFiles := make([][]*ast.File, len(config.Groups))
	groupNames := make([][]string, len(config.Groups))
	for i, group := range config.Groups {
		for _, name := range group.Sources {
			if f := selected[name]; f != nil {
				groupFiles[i] = append(groupFiles[i], f)
				groupNames[i] = append(groupNames[i], name)
			}
		}
		if len(groupFiles[i]) == 0 {
			continue
		}
		var b bytes.Buffer
		render(&b, groupNames[i], groupFiles[i], nil)
		generated, err := parser.ParseFile(fset, group.Output, b.Bytes(), 0)
		if err != nil {
			return fmt.Errorf("canonical stateify: %w", err)
		}
		canonical[i] = generated
		files = append(files, generated)
	}
	// The canonical GoStdLib provider owns these trees. Index only its compiled
	// archives, whose path below pkg/<target> is the standard import path.
	for _, root := range config.Stdlib {
		err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			if entry.IsDir() || !strings.HasSuffix(path, ".a") {
				return nil
			}
			relative, err := filepath.Rel(root, path)
			if err != nil {
				return err
			}
			_, name, ok := strings.Cut(filepath.ToSlash(relative), "/")
			if !ok {
				return fmt.Errorf("unexpected standard archive path %s", path)
			}
			name = strings.TrimSuffix(name, ".a")
			if old, exists := config.Archives[name]; exists && old.File != path {
				return fmt.Errorf("duplicate compiler exports for %s: %s and %s", name, old.File, path)
			}
			config.Archives[name] = compilerExport{File: path, ImportMap: name}
			return nil
		})
		if err != nil {
			return err
		}
	}
	info := &types.Info{Types: make(map[ast.Expr]types.TypeAndValue), Defs: make(map[*ast.Ident]types.Object), Scopes: make(map[ast.Node]*types.Scope)}
	checker := types.Config{
		Importer:  &exportImporter{fset: fset, archives: config.Archives, packages: make(map[string]*types.Package)},
		Sizes:     types.SizesFor("gc", config.GOARCH),
		GoVersion: config.GoVersion,
		// Canonical imports are pruned by the existing goimports action after
		// final rendering. Their unused presence does not affect field types.
		DisableUnusedImportCheck: true,
	}
	pkg, err := checker.Check(config.ImportPath, fset, files, info)
	if err != nil {
		return fmt.Errorf("type-check stateify package: %w", err)
	}
	for i, group := range config.Groups {
		var b bytes.Buffer
		if canonical[i] == nil {
			fmt.Fprintf(&b, "//go:build ignore\n\npackage %s\n", pkg.Name())
		} else {
			records, err := checkedRecords(canonical[i], info, pkg)
			if err != nil {
				return err
			}
			render(&b, groupNames[i], groupFiles[i], records)
		}
		if err := os.WriteFile(group.Output, b.Bytes(), 0644); err != nil {
			return err
		}
	}
	return nil
}

// checkedRecords associates compiler-checked Save arguments with the slots
// assigned by render. Custom SaveValue arguments retain their actual return
// types, which may differ from the field declaration and its load adapter.
func checkedRecords(file *ast.File, info *types.Info, pkg *types.Package) (*recordSet, error) {
	// The added import must not shadow a package type, an existing import, or
	// the receiver name reused by the canonical renderer inside StateSave.
	names := make(map[string]struct{})
	for _, name := range pkg.Scope().Names() {
		names[name] = struct{}{}
	}
	for _, name := range info.Scopes[file].Names() {
		names[name] = struct{}{}
	}
	for _, declaration := range file.Decls {
		if fn, ok := declaration.(*ast.FuncDecl); ok && fn.Recv != nil {
			for _, receiver := range fn.Recv.List {
				for _, name := range receiver.Names {
					names[name.Name] = struct{}{}
				}
			}
		}
	}
	records := &recordSet{wireAlias: unusedName("statewire", names), types: make(map[string]*recordType)}
	for _, declaration := range file.Decls {
		fn, ok := declaration.(*ast.FuncDecl)
		if !ok || fn.Recv == nil || fn.Name.Name != "StateSave" {
			continue
		}
		receiver, ok := info.Defs[fn.Name].Type().(*types.Signature).Recv().Type().(*types.Pointer)
		if !ok {
			continue
		}
		owner, ok := receiver.Elem().(*types.Named)
		if !ok {
			continue
		}
		// Identtype forwarding has no Save calls and keeps its existing path.
		slots := make(map[int]primitive)
		ast.Inspect(fn.Body, func(node ast.Node) bool {
			call, ok := node.(*ast.CallExpr)
			if !ok || len(call.Args) != 2 {
				return true
			}
			sel, ok := call.Fun.(*ast.SelectorExpr)
			if !ok {
				return true
			}
			target, ok := sel.X.(*ast.Ident)
			if !ok || target.Name != "stateSinkObject" || (sel.Sel.Name != "Save" && sel.Sel.Name != "SaveValue") {
				return true
			}
			index, ok := constant.Int64Val(info.Types[call.Args[0]].Value)
			if !ok || index < 0 {
				panic("canonical stateify produced a nonconstant slot")
			}
			typ := info.Types[call.Args[1]].Type
			if sel.Sel.Name == "Save" {
				typ = typ.(*types.Pointer).Elem()
			}
			slots[int(index)] = primitiveFor(typ)
			return true
		})
		if len(slots) == 0 {
			continue
		}
		name := owner.Obj().Name()
		record := &recordType{name: unusedName(fmt.Sprintf("stateRecord%d_%s", len(name), name), names), emitter: unusedName(fmt.Sprintf("emitStateRecord%d_%s", len(name), name), names), fields: make([]primitive, len(slots))}
		for slot := range record.fields {
			field, ok := slots[slot]
			if !ok {
				return nil, fmt.Errorf("noncontiguous saved fields for %s at %d", name, slot)
			}
			record.fields[slot] = field
		}
		records.types[name] = record
	}
	return records, nil
}

// unusedName reserves only new implementation identifiers. Existing receiver,
// method, field and import spellings are kept unchanged.
func unusedName(base string, used map[string]struct{}) string {
	name := base
	for suffix := 1; ; suffix++ {
		if _, exists := used[name]; !exists {
			used[name] = struct{}{}
			return name
		}
		name = fmt.Sprintf("%s%d", base, suffix)
	}
}
