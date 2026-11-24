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
	"fmt"
	"go/token"
	"go/types"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/tools/go/analysis"
	"golang.org/x/tools/go/gcexportdata"
	"gvisor.dev/gvisor/pkg/zip"
	"gvisor.dev/gvisor/tools/nogo/facts"
	"gvisor.dev/gvisor/tools/nogo/flags"
)

type factsLoadTestFact struct{}

func (*factsLoadTestFact) AFact() {}

func TestIgnoredFileDirectives(t *testing.T) {
	analyzers := make(map[*analysis.Analyzer]analyzer)
	for a, runner := range allAnalyzers {
		if a.Name == "directive" {
			register(analyzers, runner)
		}
	}
	dir := t.TempDir()
	source := filepath.Join(dir, "source.go")
	ignored := filepath.Join(dir, "ignored.go")
	for filename, contents := range map[string]string{
		source:  "package sample\n",
		ignored: "//go:build ignore\n\n//go:debug panicnil=1\npackage sample\n",
	} {
		if err := os.WriteFile(filename, []byte(contents), 0600); err != nil {
			t.Fatal(err)
		}
	}
	i := &importer{
		fset:      token.NewFileSet(),
		cache:     make(map[string]*importerEntry),
		analyzers: analyzers,
	}
	_, findings, _, err := i.checkPackage("sample", []string{source, ignored})
	if err != nil {
		t.Fatal(err)
	}
	if len(findings) != 1 || findings[0].Category != "directive" || findings[0].Position.Filename != ignored || !strings.Contains(findings[0].Message, "//go:debug directive only valid in package main or test") {
		t.Fatalf("findings = %v, want invalid directive in %s", findings, ignored)
	}
}

func TestFieldAlignmentLayouts(t *testing.T) {
	analyzers := make(map[*analysis.Analyzer]analyzer)
	for a, runner := range allAnalyzers {
		if a.Name == "fieldalignment" {
			register(analyzers, runner)
		}
	}
	const source = `package sample
import renamed "structs"
type Marker = renamed.HostLayout
type Different renamed.HostLayout
type ordinary struct { // report
	n uint64
	p *int
}
type host struct {
	_ Marker
	n uint64
	p *int
	child struct { // report
		n uint64
		p *int
	}
}
type notHost struct { // report
	_ Different
	n uint64
	p *int
}
// +stateify savable
type saved struct {
	n uint64
	p *int
	child struct { // report
		n uint64
		p *int
	}
}
// +stateify savable
type (
	grouped struct {
		n uint64
		p *int
	}
)
type (
	// +stateify savable
	specComment struct { // report
		n uint64
		p *int
	}
)
// +stateify savable suffix
type otherComment struct { // report
	n uint64
	p *int
}
func localType() {
	// +stateify savable
	type local struct { // report
		n uint64
		p *int
	}
}
`
	filename := filepath.Join(t.TempDir(), "source.go")
	if err := os.WriteFile(filename, []byte(source), 0600); err != nil {
		t.Fatal(err)
	}
	// The marker is identified by its declared type, including through aliases.
	structs := types.NewPackage("structs", "structs")
	marker := types.NewTypeName(token.NoPos, structs, "HostLayout", nil)
	types.NewNamed(marker, types.NewStruct(nil, nil), nil)
	structs.Scope().Insert(marker)
	structs.MarkComplete()
	i := &importer{
		fset: token.NewFileSet(),
		cache: map[string]*importerEntry{
			"structs": {pkg: structs},
		},
		analyzers: analyzers,
	}
	_, findings, _, err := i.checkPackage("sample", []string{filename})
	if err != nil {
		t.Fatal(err)
	}
	want := make(map[int]bool)
	for line, text := range strings.Split(source, "\n") {
		if strings.Contains(text, "// report") {
			want[line+1] = true
		}
	}
	for _, f := range findings {
		if f.Category != "fieldalignment" || !want[f.Position.Line] {
			t.Errorf("unexpected finding: %v", &f)
		}
		delete(want, f.Position.Line)
	}
	if len(want) != 0 {
		t.Errorf("missing fieldalignment findings at lines: %v", want)
	}
}

func TestDeclaredFactsErrors(t *testing.T) {
	savedFactMap, savedBundles := flags.FactMap, flags.Bundles
	t.Cleanup(func() {
		flags.FactMap, flags.Bundles = savedFactMap, savedBundles
	})
	var malformed bytes.Buffer
	if err := gob.NewEncoder(&malformed).Encode([]struct {
		Value any
		Key   string
	}{{Value: "bad"}}); err != nil {
		t.Fatal(err)
	}

	for _, tc := range []struct {
		wantErr  error
		name     string
		wantText string
		contents []byte
		bundles  [][]byte // A nil entry omits the package from that bundle.
		supplied bool
		create   bool
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

func TestDeprecatedImportedMethods(t *testing.T) {
	for _, exported := range []bool{false, true} {
		for _, indirect := range []bool{false, true} {
			t.Run(fmt.Sprintf("exported=%t/indirect=%t", exported, indirect), func(t *testing.T) {
				analyzers := make(map[*analysis.Analyzer]analyzer)
				for a, runner := range allAnalyzers {
					if a.Name == "SA1019" {
						register(analyzers, runner)
					}
				}
				if len(analyzers) == 0 {
					t.Fatal("SA1019 is not registered")
				}
				i := &importer{
					fset:      token.NewFileSet(),
					sources:   make(map[string][]string),
					cache:     make(map[string]*importerEntry),
					imports:   make(map[string]*types.Package),
					analyzers: analyzers,
				}
				writePackage := func(path, source string) []string {
					t.Helper()
					filename := filepath.Join(t.TempDir(), "source.go")
					if err := os.WriteFile(filename, []byte(source), 0600); err != nil {
						t.Fatal(err)
					}
					i.sources[path] = []string{filename}
					return i.sources[path]
				}
				const depPath = "gvisor.dev/gvisor/dep"
				writePackage(depPath, `package dep

type Value struct{}

// Deprecated: use New instead.
func (Value) Old() {}
func (Value) New() {}
`)
				if exported {
					pkg, err := i.importPackage(depPath, "")
					if err != nil {
						t.Fatal(err)
					}
					// Facts originate from source, but consumers normally use the
					// compiled type information. Method identities must agree.
					var typeData, factData bytes.Buffer
					if err := gcexportdata.Write(&typeData, i.fset, pkg); err != nil {
						t.Fatal(err)
					}
					if err := i.fastFacts(pkg).Serialize(&factData); err != nil {
						t.Fatal(err)
					}
					i.imports = make(map[string]*types.Package)
					pkg, err = gcexportdata.Read(&typeData, i.fset, i.imports, depPath)
					if err != nil {
						t.Fatal(err)
					}
					decoded := facts.NewPackage()
					if err := decoded.ReadFrom(pkg, &factData); err != nil {
						t.Fatal(err)
					}
					i.mu.Lock()
					i.cache[depPath] = &importerEntry{pkg: pkg, facts: decoded}
					i.mu.Unlock()
				}
				importPath, value := depPath, "dep.Value{}"
				if indirect {
					importPath, value = "gvisor.dev/gvisor/bridge", "bridge.Value()"
					writePackage(importPath, fmt.Sprintf(`package bridge
import %q
func Value() dep.Value { return dep.Value{} }
`, depPath))
				}
				source := fmt.Sprintf(`package consumer
import %q
func use() {
    value := %s
    value.Old()
    value.New()
}
`, importPath, value)
				const consumerPath = "gvisor.dev/gvisor/consumer"
				_, findings, _, err := i.checkPackage(consumerPath, writePackage(consumerPath, source))
				if err != nil {
					t.Fatal(err)
				}
				if len(findings) != 1 || findings[0].Category != "SA1019" || findings[0].Position.Line != 5 || !strings.Contains(findings[0].Message, "Old is deprecated") {
					t.Fatalf("findings = %v, want exactly the deprecated Old method on line 5", findings)
				}
			})
		}
	}
}
