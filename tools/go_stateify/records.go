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
	CgoEnabled   bool
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
	wireAlias    string
	reflectAlias string
	unsafeAlias  string
	receiver     string
	contextLocal string
	addressLocal string
	sourceLocal  string
	types        map[string]*recordType
	loaders      map[string]*loadType
	forward      map[string]string
}

// loadField records the checked destination operation, independently of the
// generated saver's capture storage. typeInfo is needed only at a dynamic or
// reference boundary, never to spell a possibly inaccessible component type.
type loadField struct {
	method   string
	typeInfo bool
}

type loadType struct {
	newValue  string
	loadValue string
	fields    []loadField
}

// loadFieldFor selects one common operation for the exact underlying kind.
// Generated code passes the actual field address; builtin names need not be
// resolved in the consumer package, where they may be shadowed.
func loadFieldFor(t types.Type) loadField {
	if basic, ok := t.Underlying().(*types.Basic); ok {
		switch basic.Kind() {
		case types.Bool:
			return loadField{method: "LoadBool"}
		case types.Int:
			return loadField{method: "LoadInt"}
		case types.Int8:
			return loadField{method: "LoadInt8"}
		case types.Int16:
			return loadField{method: "LoadInt16"}
		case types.Int32:
			return loadField{method: "LoadInt32"}
		case types.Int64:
			return loadField{method: "LoadInt64"}
		case types.Uint:
			return loadField{method: "LoadUint"}
		case types.Uint8:
			return loadField{method: "LoadUint8"}
		case types.Uint16:
			return loadField{method: "LoadUint16"}
		case types.Uint32:
			return loadField{method: "LoadUint32"}
		case types.Uint64:
			return loadField{method: "LoadUint64"}
		case types.Uintptr:
			return loadField{method: "LoadUintptr"}
		case types.Float32:
			return loadField{method: "LoadFloat32"}
		case types.Float64:
			return loadField{method: "LoadFloat64"}
		case types.Complex64:
			return loadField{method: "LoadComplex64"}
		case types.Complex128:
			return loadField{method: "LoadComplex128"}
		case types.String:
			return loadField{method: "LoadString"}
		}
	}
	if _, ok := t.Underlying().(*types.Pointer); ok {
		return loadField{method: "LoadPointer", typeInfo: true}
	}
	return loadField{method: "LoadObject", typeInfo: true}
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
			records, err := checkedRecords(canonical[i], info, pkg, models[i])
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
func checkedRecords(file *ast.File, info *types.Info, pkg *types.Package, model *groupModel) (*recordSet, error) {
	names := make(map[string]struct{})
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
		wireAlias:    unusedName("statewire", names),
		reflectAlias: unusedName("stateReflect", names),
		unsafeAlias:  unusedName("stateUnsafe", names),
		receiver:     unusedName("stateObject", names),
		contextLocal: unusedName("stateContext", names),
		addressLocal: unusedName("stateAddress", names),
		sourceLocal:  unusedName("stateSource", names),
		types:        make(map[string]*recordType),
		loaders:      make(map[string]*loadType),
		forward:      make(map[string]string),
	}

	for _, typ := range model.types {
		name := typ.spec.Name.Name
		object, ok := pkg.Scope().Lookup(name).(*types.TypeName)
		if !ok {
			return nil, fmt.Errorf("no checked type declaration for %s", name)
		}
		// A loader owns native callbacks even when StateSave is handwritten.
		var loader *loadType
		_, structural := typ.spec.Type.(*ast.StructType)
		_, structUnderlying := object.Type().Underlying().(*types.Struct)
		if typ.save && structUnderlying && (structural || typ.identical) {
			loader = &loadType{
				newValue:  unusedName(fmt.Sprintf("newStateValue%d_%s", len(name), name), names),
				loadValue: unusedName(fmt.Sprintf("loadStateValue%d_%s", len(name), name), names),
			}
			records.loaders[name] = loader
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
		record := &recordType{
			name:    unusedName(fmt.Sprintf("stateRecord%d_%s", len(name), name), names),
			factory: unusedName(fmt.Sprintf("newStateRecord%d_%s", len(name), name), names),
		}
		for _, field := range typ.fields {
			if field.slot < 0 {
				continue
			}
			if loader != nil {
				if field.slot != len(loader.fields) {
					panic("noncontiguous load field model")
				}
				loader.fields = append(loader.fields, loadField{})
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
					loader.fields[field.slot] = loadFieldFor(valueType)
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
	}
	return records, nil
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
