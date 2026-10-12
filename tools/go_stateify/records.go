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
	"go/parser"
	"go/token"
	"go/types"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"golang.org/x/tools/go/gcexportdata"
)

// packageConfig describes only declared inputs of the owning Bazel action.
// Groups preserve stateify's existing per-file-suffix hook visibility and
// outputs. Sources additionally includes independent generated Go files.
type packageConfig struct {
	Package       string
	ImportPath    string
	StatePackage  string
	Imports       []string
	Sources       []string
	Groups        []sourceGroup
	Archives      map[string]compilerExport
	NativeImports map[string]string
	Stdlib        []string
	GOOS          string
	GOARCH        string
	Tags          []string
	GoVersion     string
	CgoEnabled    bool
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
	wireAlias  string
	receiver   string
	types      map[string]*recordType
	loads      map[string]map[string]string
	forward    map[string]string
	mapTypes   []string
	mapImports []mapImport
}

type mapImport struct {
	path  string
	alias string
}

// loadHelperFor selects a shared typed entry from the checked field kind.
// Calls infer their exact named type from the field pointer, so no component
// type spelling or additional imports are needed.
func loadHelperFor(t types.Type) string {
	switch t := t.Underlying().(type) {
	case *types.Basic:
		switch t.Kind() {
		case types.Bool:
			return "LoadBool"
		case types.Int, types.Int8, types.Int16, types.Int32, types.Int64:
			return "LoadSigned"
		case types.Uint, types.Uint8, types.Uint16, types.Uint32, types.Uint64, types.Uintptr:
			return "LoadUnsigned"
		case types.Float32, types.Float64:
			return "LoadFloat"
		case types.Complex64, types.Complex128:
			return "LoadComplex"
		case types.String:
			return "LoadString"
		}
	case *types.Pointer:
		return "LoadPointer"
	case *types.Slice:
		return "LoadSlice"
	case *types.Struct:
		return "LoadStruct"
	}
	return "LoadObject"
}

type recordType struct {
	name    string
	factory string
	fields  []fieldEncoding
}

// fieldEncoding describes a field's capture storage and emitter. Scalar fields are
// unboxed and references use inline stable slots; Object represents a child
// requiring dynamic capture. No owner structs or ignored fields are copied.
type fieldEncoding struct {
	storage string
	emit    string
	capture string
}

func encodingFor(t types.Type, customValue bool) fieldEncoding {
	if b, ok := t.Underlying().(*types.Basic); ok {
		switch b.Kind() {
		case types.Bool:
			return fieldEncoding{"Bool", "SaveBoolField", ""}
		case types.Int, types.Int8, types.Int16, types.Int32, types.Int64:
			return fieldEncoding{"Int", "SaveIntField", ""}
		case types.Uint, types.Uint8, types.Uint16, types.Uint32, types.Uint64, types.Uintptr:
			return fieldEncoding{"Uint", "SaveUintField", ""}
		case types.String:
			return fieldEncoding{"String", "SaveStringField", ""}
		case types.Float32:
			// Preserve reflect.Value.Float's promotion, including signaling NaNs.
			return fieldEncoding{"Float64", "SaveFloat32Field", ""}
		case types.Float64:
			return fieldEncoding{"Float64", "SaveFloat64Field", ""}
		case types.Complex64:
			return fieldEncoding{"Complex128", "SaveComplex64Field", ""}
		case types.Complex128:
			return fieldEncoding{"Complex128", "SaveComplex128Field", ""}
		}
	}
	switch t.Underlying().(type) {
	case *types.Pointer:
		return fieldEncoding{"Ref", "SaveRefField", "CapturePointer"}
	case *types.Slice:
		return fieldEncoding{"Slice", "SaveSliceField", "CaptureSlice"}
	case *types.Map:
		return fieldEncoding{"Ref", "SaveRefField", "CaptureMap"}
	case *types.Interface:
		if !customValue {
			return fieldEncoding{"Interface", "SaveInterfaceField", "CaptureInterface"}
		}
	}
	return fieldEncoding{"Object", "Save", ""}
}

func (r *recordType) declare(w io.Writer, wireAlias string) {
	fmt.Fprintf(w, "type %s struct {\n", r.name)
	fmt.Fprintf(w, " record %s.Record\n", wireAlias)
	for i, f := range r.fields {
		fmt.Fprintf(w, " F%d %s.%s\n", i, wireAlias, f.storage)
	}
	fmt.Fprint(w, "}\n\n")
	fmt.Fprintf(w, "func %s(id %s.TypeID, count int) *%s.Record {\n", r.factory, wireAlias, wireAlias)
	fmt.Fprintf(w, " value := new(%s)\n", r.name)
	fmt.Fprintf(w, " return %s.InitRecord(&value.record, id, count, value)\n}\n\n", wireAlias)
	fmt.Fprintf(w, "func (value *%s) SaveFields(w *%s.Writer, first, last int) {\n", r.name, wireAlias)
	if len(r.fields) != 0 {
		fmt.Fprint(w, " switch first {\n")
	}
	for i, f := range r.fields {
		fmt.Fprintf(w, " case %d:\n", i)
		address := ""
		if f.capture != "" {
			address = "&"
		}
		fmt.Fprintf(w, " %s.%s(w, %svalue.F%d)\n", wireAlias, f.emit, address, i)
		if i+1 < len(r.fields) {
			fmt.Fprintf(w, " if last == %d { return }\n fallthrough\n", i+1)
		}
	}
	if len(r.fields) != 0 {
		fmt.Fprint(w, " }\n")
	}
	fmt.Fprint(w, "}\n\n")
}

func (r *recordType) capture(w io.Writer, wireAlias, statePrefix, local string, slot int, expression string, value bool) {
	f := r.fields[slot]
	if f.capture != "" {
		capture := f.capture
		address := ""
		if capture == "CaptureMap" {
			if value {
				capture = "CaptureMapValue"
			} else {
				address = "&"
			}
		}
		if capture == "CaptureInterface" {
			address = "&"
		}
		fmt.Fprintf(w, " %s%s(stateSinkObject, %s%s, &%s.F%d)\n", statePrefix, capture, address, expression, local, slot)
	} else if f.storage != "Object" {
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
	fullPkg, statePkg, imports = config.Package, config.StatePackage, strings.Join(config.Imports, ",")
	context := build.Default
	context.GOOS, context.GOARCH = config.GOOS, config.GOARCH
	context.CgoEnabled = config.CgoEnabled
	context.BuildTags = config.Tags
	fset := token.NewFileSet()
	selected := make(map[string]*ast.File)
	var files []*ast.File
	var cgoSources []string
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
		usesCgo := false
		for _, imported := range parsed[0].Imports {
			path, err := strconv.Unquote(imported.Path.Value)
			if err != nil {
				return err
			}
			usesCgo = usesCgo || path == "C"
		}
		// MatchFile checks constraints; the compiler additionally excludes
		// import-C files in pure mode, even without an explicit cgo tag.
		if usesCgo && !config.CgoEnabled {
			continue
		}
		if usesCgo {
			cgoSources = append(cgoSources, name)
		}

		selected[name] = parsed[0]
		files = append(files, parsed[0])
	}
	// Bind the shared model using declarations for methods that this package
	// will generate. The scaffold is only type-checker input; it contains no
	// production save implementation and is never compiled or rewritten.
	canonical := make([]*ast.File, len(config.Groups))
	models := make([]*groupModel, len(config.Groups))
	modelCount := 0
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
		models[i] = modelFor(groupFiles[i])
		modelCount += len(models[i].types)
		var b bytes.Buffer
		render(&b, groupNames[i], models[i], nil, true)
		generated, err := parser.ParseFile(fset, group.Output, b.Bytes(), 0)
		if err != nil {
			return fmt.Errorf("canonical stateify: %w", err)
		}
		canonical[i] = generated
		files = append(files, generated)
	}
	if modelCount == 0 {
		for i, group := range config.Groups {
			var b bytes.Buffer
			if models[i] == nil {
				_, name := filepath.Split(config.Package)
				fmt.Fprintf(&b, "//go:build ignore\n\npackage %s\n", name)
			} else {
				render(&b, groupNames[i], models[i], nil, false)
			}
			if err := os.WriteFile(group.Output, b.Bytes(), 0644); err != nil {
				return err
			}
		}
		return nil
	}
	if len(cgoSources) != 0 {
		return fmt.Errorf("stateify requires the configured cgo-generated Go inputs for annotated package %s: %s", config.ImportPath, cgoSources[0])
	}
	// The canonical GoStdLib provider owns these trees. Index only its compiled
	// archives, whose path below pkg/<target> is the standard import path.
	if config.NativeImports == nil {
		config.NativeImports = make(map[string]string)
	}
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
			config.NativeImports[name] = name
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
		IgnoreFuncBodies:         true,
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
			records, err := checkedRecords(canonical[i], info, pkg, models[i], config.NativeImports)
			if err != nil {
				return err
			}
			render(&b, groupNames[i], models[i], records, false)
		}
		if err := os.WriteFile(group.Output, b.Bytes(), 0644); err != nil {
			return err
		}
	}
	return nil
}

// checkedRecords binds the shared field model to compiler-owned types. It
// does not infer the schema from generated Save calls or field spellings.
func checkedRecords(file *ast.File, info *types.Info, pkg *types.Package, model *groupModel, nativeImports map[string]string) (*recordSet, error) {
	names := make(map[string]struct{})
	// Newly introduced import aliases must not shadow builtin spellings in
	// other map type arguments, including arguments already emitted.
	for _, name := range types.Universe.Names() {
		names[name] = struct{}{}
	}
	for _, name := range pkg.Scope().Names() {
		names[name] = struct{}{}
	}
	for _, name := range info.Scopes[file].Names() {
		names[name] = struct{}{}
	}
	for _, typ := range model.types {
		names[typ.receiver] = struct{}{}
		for _, field := range typ.fields {
			if field.kind == fieldValue {
				names[field.name+"Value"] = struct{}{}
			}
		}
	}
	records := &recordSet{
		wireAlias: unusedName("statewire", names),
		receiver:  unusedName("stateObject", names),
		types:     make(map[string]*recordType),
		loads:     make(map[string]map[string]string),
		forward:   make(map[string]string),
	}
	maps := mapDiscovery{
		pkg: pkg, scope: info.Scopes[file], imports: nativeImports,
		names: names, records: records, qualifiers: make(map[string]string),
	}
	for _, name := range info.Scopes[file].Names() {
		if imported, ok := info.Scopes[file].Lookup(name).(*types.PkgName); ok && name != "." && name != "_" {
			maps.qualifiers[imported.Imported().Path()] = name
		}
	}

	for _, typ := range model.types {
		name := typ.spec.Name.Name
		object, ok := pkg.Scope().Lookup(name).(*types.TypeName)
		if !ok {
			return nil, fmt.Errorf("no checked type declaration for %s", name)
		}
		if _, structural := typ.spec.Type.(*ast.StructType); !structural {
			if typ.save && typ.identical {
				_, records.forward[name] = resolveTypeName(typ.spec.Type)
			}
			continue
		}
		owner := types.NewPointer(object.Type())
		_, manual := model.methods[method{name, "StateSave"}]
		generatedSaver := typ.save && !manual
		loadHelpers := make(map[string]string)
		record := &recordType{
			name:    unusedName(fmt.Sprintf("stateRecord%d_%s", len(name), name), names),
			factory: unusedName(fmt.Sprintf("newStateRecord%d_%s", len(name), name), names),
		}
		for _, field := range typ.fields {
			if field.slot < 0 {
				continue
			}
			var valueType types.Type
			lookup := field.name
			if field.kind == fieldValue {
				lookup = "save" + camelCased(field.name)
			}
			member, _, _ := types.LookupFieldOrMethod(owner, true, pkg, lookup)
			if member == nil {
				if !generatedSaver && field.kind == fieldValue {
					continue
				}
				return nil, fmt.Errorf("%s has no checked member %s", name, lookup)
			}
			if field.kind == fieldValue {
				signature, ok := member.Type().(*types.Signature)
				if !ok || signature.Results().Len() != 1 {
					if !generatedSaver {
						continue
					}
					return nil, fmt.Errorf("%s.%s must return exactly one saved value", name, lookup)
				}
				valueType = signature.Results().At(0).Type()
			} else {
				valueType = member.Type()
				if typ.save {
					if _, ok := member.(*types.Var); !ok {
						return nil, fmt.Errorf("%s.%s is not a checked Go field", name, lookup)
					}
					loadHelpers[field.name] = loadHelperFor(valueType)
					maps.collect(valueType)
				}
			}
			if generatedSaver && field.slot != len(record.fields) {
				panic("noncontiguous field model")
			}
			if generatedSaver {
				record.fields = append(record.fields, encodingFor(types.Unalias(valueType), field.kind == fieldValue))
			}
		}
		if generatedSaver {
			records.types[name] = record
		}
		if len(loadHelpers) != 0 {
			records.loads[name] = loadHelpers
		}
	}
	return records, nil
}

// mapDiscovery visits only types reachable from generated Load destinations.
// It never discovers fields inside a struct with its own loader. Traversal and
// emission have separate identities: an inaccessible spelling must not suppress
// a later accessible alias for the same exact map type.
type mapDiscovery struct {
	pkg        *types.Package
	scope      *types.Scope
	imports    map[string]string
	names      map[string]struct{}
	records    *recordSet
	qualifiers map[string]string
	visited    []types.Type
	emitted    []types.Type
}

func containsCheckedType(typesSeen []types.Type, typ types.Type) bool {
	for _, seen := range typesSeen {
		if types.Identical(seen, typ) {
			return true
		}
	}
	return false
}

func (d *mapDiscovery) collect(typ types.Type) {
	identity := types.Unalias(typ)
	if _, ok := identity.Underlying().(*types.Map); ok && !containsCheckedType(d.emitted, identity) && d.nameable(typ) {
		d.records.mapTypes = append(d.records.mapTypes, types.TypeString(typ, d.qualify))
		d.emitted = append(d.emitted, identity)
	}
	if containsCheckedType(d.visited, identity) {
		return
	}
	d.visited = append(d.visited, identity)
	switch t := identity.Underlying().(type) {
	case *types.Map:
		d.collect(t.Key())
		d.collect(t.Elem())
	case *types.Pointer:
		d.collect(t.Elem())
	case *types.Slice:
		d.collect(t.Elem())
	case *types.Array:
		d.collect(t.Elem())
	}
}

func (d *mapDiscovery) qualify(pkg *types.Package) string {
	if pkg == d.pkg {
		return ""
	}
	if alias := d.qualifiers[pkg.Path()]; alias != "" {
		return alias
	}
	alias := unusedName(pkg.Name(), d.names)
	d.qualifiers[pkg.Path()] = alias
	d.records.mapImports = append(d.records.mapImports, mapImport{path: d.imports[pkg.Path()], alias: alias})
	return alias
}

func (d *mapDiscovery) named(object *types.TypeName, arguments *types.TypeList) bool {
	if pkg := object.Pkg(); pkg != nil && pkg != d.pkg {
		if !object.Exported() || d.imports[pkg.Path()] == "" {
			return false
		}
	} else {
		for _, scope := range []*types.Scope{d.scope, d.pkg.Scope()} {
			if shadow := scope.Lookup(object.Name()); shadow != nil && shadow != object {
				return false
			}
		}
	}
	for i := 0; i < arguments.Len(); i++ {
		if !d.nameable(arguments.At(i)) {
			return false
		}
	}
	return true
}

// nameable checks only the syntax that TypeString will emit. A named map can
// hide inaccessible components: RegisterMap infers them from that map's type.
// Anonymous foreign structs/interfaces with inaccessible members are left at
// the reflection boundary rather than inventing an inequivalent Go spelling.
func (d *mapDiscovery) nameable(typ types.Type) bool {
	switch t := typ.(type) {
	case *types.Alias:
		return d.named(t.Obj(), t.TypeArgs())
	case *types.Named:
		return d.named(t.Obj(), t.TypeArgs())
	case *types.Basic:
		if t.Kind() == types.UnsafePointer || t.Info()&types.IsUntyped != 0 {
			return false
		}
		for _, scope := range []*types.Scope{d.scope, d.pkg.Scope()} {
			if object := scope.Lookup(t.Name()); object != nil {
				name, ok := object.(*types.TypeName)
				if !ok || !types.Identical(name.Type(), t) {
					return false
				}
			}
		}
		return true
	case *types.Pointer:
		return d.nameable(t.Elem())
	case *types.Slice:
		return d.nameable(t.Elem())
	case *types.Array:
		return d.nameable(t.Elem())
	case *types.Map:
		return d.nameable(t.Key()) && d.nameable(t.Elem())
	case *types.Chan:
		return d.nameable(t.Elem())
	case *types.Struct:
		for i := 0; i < t.NumFields(); i++ {
			field := t.Field(i)
			if (!field.Exported() && field.Pkg() != d.pkg) || !d.nameable(field.Type()) {
				return false
			}
		}
		return true
	case *types.Interface:
		for i := 0; i < t.NumExplicitMethods(); i++ {
			method := t.ExplicitMethod(i)
			if (!method.Exported() && method.Pkg() != d.pkg) || !d.nameable(method.Type()) {
				return false
			}
		}
		for i := 0; i < t.NumEmbeddeds(); i++ {
			if !d.nameable(t.EmbeddedType(i)) {
				return false
			}
		}
		return true
	case *types.Signature:
		if t.TypeParams().Len() != 0 {
			return false
		}
		for _, tuple := range []*types.Tuple{t.Params(), t.Results()} {
			for i := 0; i < tuple.Len(); i++ {
				if !d.nameable(tuple.At(i).Type()) {
					return false
				}
			}
		}
		return true
	default:
		return false
	}
}

// unusedName reserves a new generated identifier against existing scope names.
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
