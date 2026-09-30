// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package check

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"go/types"
	"io"
	"os"
	"runtime"
	"strings"
	"sync"
	"testing"

	"golang.org/x/tools/go/analysis"
	"gvisor.dev/gvisor/tools/nogo/facts"
	"gvisor.dev/gvisor/tools/nogo/flags"
)

// These fork-only measurements reuse the real importer, generated sources,
// buildssa result, archives and this analyzer version's dependency facts.
func benchmarkCheckescapePackage(path string) bool {
	if flags.GOARCH != "amd64" {
		return false
	}
	switch path {
	case "gvisor.dev/gvisor/pkg/bitmap", "gvisor.dev/gvisor/pkg/tcpip/header",
		"gvisor.dev/gvisor/tools/checkescape/test1", "gvisor.dev/gvisor/tools/checkescape/test2":
		return true
	default:
		return false
	}
}

func benchmarkCheckescape(a analyzer, original *analysis.Pass, archive io.Reader) error {
	runner, ok := a.(interface {
		RunBenchmark(*analysis.Pass, io.Reader) (any, error)
	})
	if !ok {
		return fmt.Errorf("checkescape lacks diagnostic benchmark entry point")
	}
	file, ok := archive.(*os.File)
	if !ok {
		return fmt.Errorf("checkescape benchmark requires an open archive file")
	}

	// Preserve shared astFacts and findings belonging to the ordinary analyzer
	// run. Each timed iteration gets empty outputs; dependency imports retain
	// the driver's real callbacks and same-version facts.
	pass := *original
	var output *facts.Package
	var findings FindingSet
	var factsMu sync.Mutex
	pass.ExportObjectFact = func(obj types.Object, fact analysis.Fact) {
		if obj == nil || obj.Pkg() != pass.Pkg {
			return
		}
		factsMu.Lock()
		defer factsMu.Unlock()
		output.ExportFact(obj, fact)
	}
	pass.ImportObjectFact = func(obj types.Object, fact analysis.Fact) bool {
		if obj.Pkg() != nil && obj.Pkg() != pass.Pkg {
			return original.ImportObjectFact(obj, fact)
		}
		factsMu.Lock()
		defer factsMu.Unlock()
		return output.ImportFact(obj, fact)
	}
	pass.Report = func(d analysis.Diagnostic) {
		findings = append(findings, Finding{
			Category: pass.Analyzer.Name,
			Position: pass.Fset.Position(d.Pos),
			Message:  d.Message,
			GOOS:     flags.GOOS,
			GOARCH:   flags.GOARCH,
		})
	}
	run := func() error {
		output = facts.NewPackage()
		findings = nil
		if _, err := file.Seek(0, io.SeekStart); err != nil {
			return err
		}
		_, err := runner.RunBenchmark(&pass, file)
		return err
	}

	// One untimed real run warms lazy fact imports and Go's objdump tool cache.
	// The timed entry point independently rejects disassembly failure every time.
	previousProcs := runtime.GOMAXPROCS(1)
	defer runtime.GOMAXPROCS(previousProcs)
	if err := run(); err != nil {
		return fmt.Errorf("checkescape benchmark warm-up: %w", err)
	}
	inputs := make(map[string]string)
	var sources []string
	addInput := func(name string) error {
		f, err := os.Open(name)
		if err != nil {
			return err
		}
		defer f.Close()
		h := sha256.New()
		if _, err := io.Copy(h, f); err != nil {
			return err
		}
		inputs[name] = fmt.Sprintf("%x", h.Sum(nil))
		return nil
	}
	for _, source := range pass.Files {
		name := pass.Fset.Position(source.Pos()).Filename
		sources = append(sources, name)
		if err := addInput(name); err != nil {
			return err
		}
	}
	for _, name := range append([]string{file.Name(), flags.Go}, flags.Bundles...) {
		if err := addInput(name); err != nil {
			return err
		}
	}
	for _, name := range flags.FactMap {
		if err := addInput(name); err != nil {
			return err
		}
	}
	metadata, err := json.Marshal(struct {
		Package     string            `json:"package"`
		GoVersion   string            `json:"go_version"`
		GOOS        string            `json:"goos"`
		GOARCH      string            `json:"goarch"`
		GOMAXPROCS  int               `json:"gomaxprocs"`
		Findings    int               `json:"findings"`
		FactObjects int               `json:"fact_objects"`
		Archive     string            `json:"archive"`
		Sources     []string          `json:"sources"`
		GoBinary    string            `json:"go_binary"`
		Inputs      map[string]string `json:"inputs_sha256"`
	}{pass.Pkg.Path(), flags.GOVERSION, flags.GOOS, flags.GOARCH, 1, len(findings), len(output.Objects), file.Name(), sources, flags.Go, inputs})
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "CHECKESCAPE_BENCHMARK_INPUTS %s\n", metadata)

	// This is a non-test executable, so explicitly initialize testing's default
	// one-second benchmark duration. Compilation, parsing, type checking and SSA
	// preparation are outside this boundary. Each iteration includes resetting
	// outputs and the real objdump child; allocation metrics cover this Go process.
	testing.Init()
	name := strings.TrimPrefix(pass.Pkg.Path(), "gvisor.dev/gvisor/")
	for sample := range 10 {
		var runErr error
		result := testing.Benchmark(func(b *testing.B) {
			b.ReportAllocs()
			for b.Loop() {
				if runErr = run(); runErr != nil {
					b.Fatal(runErr)
				}
			}
		})
		if runErr != nil {
			return fmt.Errorf("checkescape benchmark sample %d: %w", sample, runErr)
		}
		fmt.Fprintf(os.Stderr, "BenchmarkCheckescape/%s %s\t%s\n", name, result.String(), result.MemString())
	}
	_, err = file.Seek(0, io.SeekStart)
	return err
}
