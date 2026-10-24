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

package check

import (
	"bytes"
	"encoding/gob"
	"errors"
	"go/token"
	"go/types"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/tools/go/analysis"
	"gvisor.dev/gvisor/pkg/zip"
	"gvisor.dev/gvisor/tools/nogo/flags"
)

type factsLoadTestFact struct{}

func (*factsLoadTestFact) AFact() {}

func TestDeclaredFactsErrors(t *testing.T) {
	savedFactMap, savedBundles := flags.FactMap, flags.Bundles
	t.Cleanup(func() {
		flags.FactMap, flags.Bundles = savedFactMap, savedBundles
	})
	var malformed bytes.Buffer
	if err := gob.NewEncoder(&malformed).Encode([]struct {
		Key   string
		Value any
	}{{Value: "bad"}}); err != nil {
		t.Fatal(err)
	}

	for _, tc := range []struct {
		name     string
		supplied bool
		create   bool
		contents []byte
		bundles  [][]byte // A nil entry omits the package from that bundle.
		wantErr  error
		wantText string
	}{
		{name: "absent"},
		{name: "missing", supplied: true, wantErr: os.ErrNotExist},
		{name: "empty", supplied: true, create: true, wantErr: io.EOF},
		{name: "wrong_type", supplied: true, create: true, contents: malformed.Bytes(), wantText: "invalid fact payload"},
		{name: "bundle_absent", bundles: [][]byte{nil}},
		{name: "bundle_empty", bundles: [][]byte{{}}, wantErr: io.EOF},
		{name: "later_bundle_wrong_type", bundles: [][]byte{nil, malformed.Bytes()}, wantText: "invalid fact payload"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dep := types.NewPackage("indirect", "indirect")
			flags.FactMap = make(map[string]string)
			flags.Bundles = nil
			if tc.supplied {
				filename := filepath.Join(t.TempDir(), "facts")
				flags.FactMap[dep.Path()] = filename
				if tc.create {
					if err := os.WriteFile(filename, tc.contents, 0600); err != nil {
						t.Fatal(err)
					}
				}
			}
			for _, contents := range tc.bundles {
				var buf bytes.Buffer
				zw := zip.NewWriter(&buf)
				if contents != nil {
					w, err := zw.Create(dep.Path())
					if err != nil {
						t.Fatal(err)
					}
					if _, err := w.Write(contents); err != nil {
						t.Fatal(err)
					}
				}
				if err := zw.Close(); err != nil {
					t.Fatal(err)
				}
				filename := filepath.Join(t.TempDir(), "bundle.zip")
				if err := os.WriteFile(filename, buf.Bytes(), 0600); err != nil {
					t.Fatal(err)
				}
				flags.Bundles = append(flags.Bundles, filename)
			}
			a := &analysis.Analyzer{
				Name:      "importfact",
				FactTypes: []analysis.Fact{new(factsLoadTestFact)},
				Run: func(p *analysis.Pass) (any, error) {
					// Fact absence is allowed; input errors must fail the driver.
					_ = p.ImportPackageFact(dep, new(factsLoadTestFact))
					return nil, nil
				},
			}
			i := &importer{
				fset:      token.NewFileSet(),
				cache:     make(map[string]*importerEntry),
				analyzers: map[*analysis.Analyzer]analyzer{a: &plainAnalyzer{a}},
			}
			// An empty package exercises fact loading without SDK imports.
			_, findings, _, err := i.checkPackage("test", nil)
			if tc.wantText != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantText) {
					t.Fatalf("checkPackage() error = %v, want %q (findings: %v)", err, tc.wantText, findings)
				}
			} else if !errors.Is(err, tc.wantErr) {
				t.Fatalf("checkPackage() error = %v, want %v", err, tc.wantErr)
			}
			if err != nil && !strings.Contains(err.Error(), dep.Path()) {
				t.Errorf("checkPackage() error = %v, want package path %q", err, dep.Path())
			}
		})
	}
}

func TestExportDataInputs(t *testing.T) {
	savedImports, savedPaths, savedStdlib := flags.ImportMap, flags.ImportPaths, flags.Stdlib
	t.Cleanup(func() {
		flags.ImportMap, flags.ImportPaths, flags.Stdlib = savedImports, savedPaths, savedStdlib
	})
	const canonical = "example.org/private/dep"
	for _, tc := range []struct {
		name    string
		data    string
		stdlib  bool
		missing bool
		wantErr bool
	}{
		{name: "direct"},
		{name: "stdlib", stdlib: true},
		{name: "missing", missing: true, wantErr: true},
		{name: "malformed", data: "invalid", wantErr: true},
		{name: "compiler_archive", data: "!<arch>\n", wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			flags.ImportMap = make(map[string]string)
			flags.ImportPaths = map[string]string{"alias": canonical}
			flags.Stdlib = t.TempDir()
			filename := filepath.Join(flags.Stdlib, canonical+".export")
			if !tc.missing {
				pkg := types.NewPackage(canonical, "dep")
				pkg.MarkComplete()
				if err := writeExport(filename, token.NewFileSet(), pkg); err != nil {
					t.Fatal(err)
				}
				if tc.data != "" {
					if err := os.WriteFile(filename, []byte(tc.data), 0600); err != nil {
						t.Fatal(err)
					}
				}
			}
			if !tc.stdlib {
				flags.ImportMap[canonical] = filename
			}
			i := &importer{fset: token.NewFileSet(), cache: make(map[string]*importerEntry), imports: make(map[string]*types.Package)}
			pkg, err := i.Import("alias")
			if (err != nil) != tc.wantErr {
				t.Fatalf("Import() = %v, %v; want error: %t", pkg, err, tc.wantErr)
			}
			if !tc.wantErr {
				if got, want := pkg.Path(), canonical; got != want {
					t.Errorf("package path = %q, want %q", got, want)
				}
			}
		})
	}
}

func TestExportTargetSizes(t *testing.T) {
	savedArch := flags.GOARCH
	t.Cleanup(func() { flags.GOARCH = savedArch })
	src := filepath.Join(t.TempDir(), "source.go")
	if err := os.WriteFile(src, []byte(`package sizes
import "unsafe"
const Word = unsafe.Sizeof(uintptr(0))
type Value [Word]byte
`), 0600); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		arch string
		size int64
	}{{"amd64", 8}, {"arm64", 8}, {"386", 4}} {
		t.Run(tc.arch, func(t *testing.T) {
			flags.GOARCH = tc.arch
			i := &importer{fset: token.NewFileSet(), cache: make(map[string]*importerEntry), imports: make(map[string]*types.Package)}
			pkg, _, _, err := i.checkPackage("sizes", []string{src})
			if err != nil {
				t.Fatal(err)
			}
			filename := filepath.Join(t.TempDir(), "sizes.export")
			if err := writeExport(filename, i.fset, pkg); err != nil {
				t.Fatal(err)
			}
			savedImports := flags.ImportMap
			flags.ImportMap = map[string]string{"sizes": filename}
			defer func() { flags.ImportMap = savedImports }()
			consumer := &importer{fset: token.NewFileSet(), cache: make(map[string]*importerEntry), imports: make(map[string]*types.Package)}
			loaded, err := consumer.Import("sizes")
			if err != nil {
				t.Fatal(err)
			}
			if got, want := loaded.Scope().Lookup("Value").Type().Underlying().(*types.Array).Len(), tc.size; got != want {
				t.Errorf("exported array length = %d, want %d", got, want)
			}
		})
	}
}

func TestBundleAndPackageExports(t *testing.T) {
	savedAnalyzers, savedStdlib := allAnalyzers, flags.Stdlib
	t.Cleanup(func() {
		allAnalyzers, flags.Stdlib = savedAnalyzers, savedStdlib
	})
	// This fixture checks the export protocol independently of individual
	// analyzers, which have their own binary and fact inputs.
	allAnalyzers = nil
	dir := t.TempDir()
	sources := make(map[string][]string)
	for path, source := range map[string]string{
		"internal/abi": "package abi; type MapType struct { Size uintptr }",
		"outer": `package outer
import "internal/abi"
type Value[T any] struct { Map abi.MapType; Data T }
`,
	} {
		filename := filepath.Join(dir, path, "source.go")
		if err := os.MkdirAll(filepath.Dir(filename), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filename, []byte(source), 0600); err != nil {
			t.Fatal(err)
		}
		sources[path] = []string{filename}
	}
	flags.Stdlib = filepath.Join(dir, "exports")
	if _, _, err := Bundle(sources, nil, flags.Stdlib); err != nil {
		t.Fatal(err)
	}
	consumer := filepath.Join(dir, "consumer.go")
	if err := os.WriteFile(consumer, []byte(`package consumer
import "outer"
type Value = outer.Value[string]
`), 0600); err != nil {
		t.Fatal(err)
	}
	var exported bytes.Buffer
	if _, _, err := Package("consumer", []string{consumer}, &exported); err != nil {
		t.Fatal(err)
	}
	// Package's consumer preloads internal/abi before importing outer. Its
	// exported generic alias must still refer to that one canonical package.
	filename := filepath.Join(flags.Stdlib, "consumer.export")
	if err := os.WriteFile(filename, exported.Bytes(), 0600); err != nil {
		t.Fatal(err)
	}
	i := &importer{fset: token.NewFileSet(), cache: make(map[string]*importerEntry), imports: make(map[string]*types.Package)}
	pkg, err := i.Import("consumer")
	if err != nil {
		t.Fatal(err)
	}
	internal, err := i.Import("internal/abi")
	if err != nil {
		t.Fatal(err)
	}
	value := pkg.Scope().Lookup("Value").Type().Underlying().(*types.Struct)
	if got, want := value.Field(0).Type().(*types.Named).Obj().Pkg(), internal; got != want {
		t.Errorf("exported field package = %p, want canonical internal package %p", got, want)
	}
}
