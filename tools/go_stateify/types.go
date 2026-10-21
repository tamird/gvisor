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
	Sources      []string
	Output       string
	Declarations string
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
	// TODO: Replace this compiler-archive adapter with declared exports from
	// the source exporter before delivering the destination decoder change.
	reader, err := gcexportdata.NewReader(f)
	if err != nil {
		return nil, err
	}
	return gcexportdata.Read(reader, i.fset, i.packages, export.ImportMap)
}

// generate resolves the configured package once, using the same generated
// method declarations that are compiled later. Method bodies are not checked.
func generate(configPath string, declarationsOnly bool) error {
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
	context.CgoEnabled, context.BuildTags = config.CgoEnabled, config.Tags
	fset := token.NewFileSet()
	selected := make(map[string]*ast.File)
	var files []*ast.File
	var cgoSources []string
	for _, name := range config.Sources {
		if filepath.Ext(name) != ".go" || selected[name] != nil {
			continue
		}
		matches, err := context.MatchFile(filepath.Dir(name), filepath.Base(name))
		if err != nil {
			return err
		}
		if !matches {
			continue
		}
		file, err := parser.ParseFile(fset, name, nil, parser.ParseComments)
		if err != nil {
			return err
		}
		usesCgo := false
		for _, imported := range file.Imports {
			path, err := strconv.Unquote(imported.Path.Value)
			if err != nil {
				return err
			}
			usesCgo = usesCgo || path == "C"
		}
		if usesCgo && !config.CgoEnabled {
			continue
		}
		if usesCgo {
			cgoSources = append(cgoSources, name)
		}
		selected[name] = file
		files = append(files, file)
	}
	groupFiles := make([][]*ast.File, len(config.Groups))
	groupNames := make([][]string, len(config.Groups))
	canonical := make([]*ast.File, len(config.Groups))
	outputs := make([][]byte, len(config.Groups))
	hasTypes := false
	for i, group := range config.Groups {
		for _, name := range group.Sources {
			if file := selected[name]; file != nil {
				groupFiles[i] = append(groupFiles[i], file)
				groupNames[i] = append(groupNames[i], name)
			}
		}
		var out bytes.Buffer
		if len(groupFiles[i]) == 0 {
			_, name := filepath.Split(config.Package)
			fmt.Fprintf(&out, "//go:build ignore\n\npackage %s\n", name)
		} else {
			if err := render(&out, groupNames[i], groupFiles[i], renderHooks{typeInfo: func() { hasTypes = true }}); err != nil {
				return err
			}
			if !declarationsOnly {
				// Rendered import hints can be obsolete or unused. Type-check
				// the same declarations after the canonical goimports action,
				// which removes those hints before resolving compiler exports.
				file, err := parser.ParseFile(fset, group.Declarations, nil, 0)
				if err != nil {
					return fmt.Errorf("stateify declarations: %w", err)
				}
				canonical[i] = file
				files = append(files, file)
			}
		}
		outputs[i] = out.Bytes()
	}
	if !declarationsOnly && hasTypes {
		if len(cgoSources) != 0 {
			return fmt.Errorf("stateify needs configured cgo-generated inputs for %s: %s", config.ImportPath, cgoSources[0])
		}
		// These declared GoStdLib trees contain pkg/<target>/<import>.a.
		for _, root := range config.Stdlib {
			if err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
				if err != nil {
					return err
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
					return fmt.Errorf("duplicate compiler exports for %s", name)
				}
				config.Archives[name] = compilerExport{File: path, ImportMap: name}
				return nil
			}); err != nil {
				return err
			}
		}
		info := &types.Info{Defs: make(map[*ast.Ident]types.Object), Types: make(map[ast.Expr]types.TypeAndValue)}
		checker := types.Config{
			Importer:         &exportImporter{fset: fset, archives: config.Archives, packages: make(map[string]*types.Package)},
			Sizes:            types.SizesFor("gc", config.GOARCH),
			GoVersion:        config.GoVersion,
			IgnoreFuncBodies: true,
		}
		_, err = checker.Check(config.ImportPath, fset, files, info)
		if err != nil {
			return fmt.Errorf("type-check stateify package: %w", err)
		}
		for i := range config.Groups {
			if canonical[i] == nil {
				continue
			}
			var out bytes.Buffer
			// Diagnostic control: keep the configured checker and exact fixture,
			// but select the existing reflective Source.Load/LoadWait renderer.
			if err := render(&out, groupNames[i], groupFiles[i], renderHooks{}); err != nil {
				return err
			}
			outputs[i] = out.Bytes()
		}
	}
	for i, group := range config.Groups {
		if err := os.WriteFile(group.Output, outputs[i], 0644); err != nil {
			return err
		}
	}
	return nil
}
