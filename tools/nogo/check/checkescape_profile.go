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
	"reflect"
	"runtime"
	"runtime/pprof"
	"sync"

	"golang.org/x/tools/go/analysis"
	"gvisor.dev/gvisor/tools/nogo/facts"
	"gvisor.dev/gvisor/tools/nogo/flags"
)

// These fork-only profiles reuse the prepared real pass and strict disassembly.
const checkescapeProfileIterations = 20

func profileCheckescapePackage(path string) bool {
	return flags.GOARCH == "amd64" && path == "gvisor.dev/gvisor/pkg/tcpip/header" && os.Getenv("CHECKESCAPE_PROFILE_MODE") != ""
}

func profileCheckescape(a analyzer, original *analysis.Pass, archive io.Reader) error {
	runner, ok := a.(interface {
		RunBenchmark(*analysis.Pass, io.Reader) (any, error)
	})
	if !ok {
		return fmt.Errorf("checkescape lacks diagnostic benchmark entry point")
	}
	file, ok := archive.(*os.File)
	if !ok {
		return fmt.Errorf("checkescape profile requires an open archive file")
	}

	// Preserve shared astFacts and findings belonging to the ordinary analyzer
	// run. Each profiled iteration gets empty outputs; dependency imports retain
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
	// The profiled entry point independently rejects disassembly failure every time.
	previousProcs := runtime.GOMAXPROCS(1)
	defer runtime.GOMAXPROCS(previousProcs)
	if err := run(); err != nil {
		return fmt.Errorf("checkescape profile warm-up: %w", err)
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
	mode := os.Getenv("CHECKESCAPE_PROFILE_MODE")
	profilePath := os.Getenv("CHECKESCAPE_PROFILE_OUTPUT")
	baselinePath := os.Getenv("CHECKESCAPE_PROFILE_BASELINE")
	if profilePath == "" || (mode != "cpu" && mode != "allocs") {
		return fmt.Errorf("invalid checkescape profile configuration")
	}
	if mode == "allocs" && (baselinePath == "" || runtime.MemProfileRate != 1) {
		return fmt.Errorf("allocation profiling requires full sampling from process startup")
	}
	runFunction := runtime.FuncForPC(reflect.ValueOf(run).Pointer()).Name()
	metadata, err := json.Marshal(struct {
		Package        string            `json:"package"`
		GoVersion      string            `json:"go_version"`
		GOOS           string            `json:"goos"`
		GOARCH         string            `json:"goarch"`
		GOMAXPROCS     int               `json:"gomaxprocs"`
		Findings       int               `json:"findings"`
		FactObjects    int               `json:"fact_objects"`
		Archive        string            `json:"archive"`
		Sources        []string          `json:"sources"`
		GoBinary       string            `json:"go_binary"`
		Inputs         map[string]string `json:"inputs_sha256"`
		Mode           string            `json:"mode"`
		Iterations     int               `json:"iterations"`
		MemProfileRate int               `json:"mem_profile_rate"`
		RunFunction    string            `json:"run_function"`
		Profile        string            `json:"profile"`
		Baseline       string            `json:"baseline,omitempty"`
	}{pass.Pkg.Path(), flags.GOVERSION, flags.GOOS, flags.GOARCH, 1, len(findings), len(output.Objects), file.Name(), sources, flags.Go, inputs, mode, checkescapeProfileIterations, runtime.MemProfileRate, runFunction, profilePath, baselinePath})
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "CHECKESCAPE_PROFILE_INPUTS %s\n", metadata)

	runAll := func() error {
		for iteration := range checkescapeProfileIterations {
			if err := run(); err != nil {
				return fmt.Errorf("checkescape profile iteration %d: %w", iteration, err)
			}
		}
		return nil
	}
	if mode == "cpu" {
		f, err := os.Create(profilePath)
		if err != nil {
			return err
		}
		if err := pprof.StartCPUProfile(f); err != nil {
			f.Close()
			return err
		}
		runErr := runAll()
		pprof.StopCPUProfile()
		closeErr := f.Close()
		if runErr != nil {
			return runErr
		}
		if closeErr != nil {
			return closeErr
		}
	} else {
		// GC flushes allocation profiling in the pinned runtime. Subtract this
		// cumulative baseline to exclude parse/typecheck/SSA and warm-up work.
		// Baseline serialization remains in the process delta; run-stack focus
		// excludes it from analyzer-attributed totals.
		if err := writeCheckescapeAllocProfile(baselinePath); err != nil {
			return err
		}
		if err := runAll(); err != nil {
			return err
		}
		if err := writeCheckescapeAllocProfile(profilePath); err != nil {
			return err
		}
	}
	fmt.Fprintf(os.Stderr, "CHECKESCAPE_PROFILE_COMPLETE %s\n", metadata)
	_, err = file.Seek(0, io.SeekStart)
	return err
}

func writeCheckescapeAllocProfile(path string) error {
	runtime.GC()
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	writeErr := pprof.Lookup("allocs").WriteTo(f, 0)
	closeErr := f.Close()
	if writeErr != nil {
		return writeErr
	}
	return closeErr
}
