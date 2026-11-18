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

package kvm_test

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/sentry/platform/kvm/codegen"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

type codegenRecord struct {
	Arch string `json:"arch"`
	Artifacts map[string]string `json:"artifacts"`
	Sources []string `json:"sources"`
}

type codegenInputs struct {
	Go string `json:"go"`
	SDK string `json:"sdk"`
	Records []codegenRecord `json:"records"`
}

func codegenFile(t *testing.T, path string) string {
	t.Helper()
	value, err := testutil.FindFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return value
}

func codegenSource(t *testing.T, record codegenRecord, suffix string) []byte {
	t.Helper()
	var paths []string
	for _, path := range record.Sources {
		if path == suffix || strings.HasSuffix(path, "/"+suffix) {
			paths = append(paths, path)
		}
	}
	if len(paths) != 1 {
		t.Fatalf("source %s: expected one input, got %v", suffix, paths)
	}
	data, err := os.ReadFile(codegenFile(t, paths[0]))
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestAtomicPtrProductionCodegen(t *testing.T) {
	path := codegenFile(t, "pkg/sentry/platform/kvm/atomicptr_codegen_inputs.json")
	file, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	var inputs codegenInputs
	decoder := json.NewDecoder(file)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&inputs); err != nil {
		t.Fatal(err)
	}
	if inputs.SDK != runtime.Version() || len(inputs.Records) != 2 {
		t.Fatalf("unexpected configured SDK/architectures: %+v; inspector=%s", inputs, runtime.Version())
	}
	goTool := codegenFile(t, inputs.Go)
	seen := make(map[string]bool)
	for _, record := range inputs.Records {
		if (record.Arch != "amd64" && record.Arch != "arm64") || seen[record.Arch] {
			t.Fatalf("unexpected or repeated architecture %q", record.Arch)
		}
		seen[record.Arch] = true
		t.Run(record.Arch, func(t *testing.T) {
			report := make(map[string]any)
			report["sdk"] = inputs.SDK
			report["arch"] = record.Arch
			t.Cleanup(func() {
				directory := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
				if directory == "" {
					directory = t.TempDir()
				}
				data, err := json.MarshalIndent(report, "", "  ")
				if err != nil {
					t.Error(err)
					return
				}
				if err := os.WriteFile(filepath.Join(directory, record.Arch+"-atomicptr-codegen.json"), append(data, '\n'), 0600); err != nil {
					t.Error(err)
				}
			})
			artifacts := make(map[string]string)
			hashes := make(map[string]string)
			for _, name := range []string{"elf", "kernel_archive", "kvm_archive", "negative_archive"} {
				value, ok := record.Artifacts[name]
				if !ok {
					t.Fatalf("missing %s input", name)
				}
				artifacts[name] = codegenFile(t, value)
				data, err := os.ReadFile(artifacts[name])
				if err != nil {
					t.Fatal(err)
				}
				hashes[name] = fmt.Sprintf("%x", sha256.Sum256(data))
			}
			report["artifactSHA256"] = hashes
			const handler = "pkg/sentry/platform/kvm/seccomp_mmap_unsafe.go"
			const descriptors = "pkg/sentry/kernel/fd_table_unsafe.go"
			var sites []codegen.Site
			sourceHashes := make(map[string]string)
			report["sourceSHA256"] = sourceHashes
			for _, source := range []string{handler, descriptors} {
				data := codegenSource(t, record, source)
				sourceHashes[source] = fmt.Sprintf("%x", sha256.Sum256(data))
				bound, err := codegen.Sites(source, data)
				if err != nil {
					t.Fatal(err)
				}
				sites = append(sites, bound...)
			}
			report["requiredSites"] = sites
			for _, declaration := range []struct { file, function string }{{handler, "seccompMmapHandler"}, {"atomicptr_machine.go", "Load"}} {
				if err := codegen.NoSplit(declaration.file, codegenSource(t, record, declaration.file), declaration.function); err != nil {
					t.Fatal(err)
				}
			}
			records, err := codegen.Read(artifacts["elf"], record.Arch)
			report["liveInlineRecords"] = records
			if err != nil {
				t.Fatal(err)
			}
			if err := codegen.Validate(records, sites); err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(t.Context(), 2*time.Minute)
			defer cancel()
			const handlerSymbol = "gvisor.dev/gvisor/pkg/sentry/platform/kvm.seccompMmapHandler"
			owners := map[string][]string{
				"kvm_archive": {handlerSymbol},
				"kernel_archive": {"gvisor.dev/gvisor/pkg/sentry/kernel.(*FDTable).get", "gvisor.dev/gvisor/pkg/sentry/kernel.(*FDTable).swap"},
			}
			for _, name := range []string{"kvm_archive", "kernel_archive"} {
				calls, err := codegen.Calls(ctx, artifacts[name], goTool, owners[name])
				report[name+"Calls"] = calls
				if err != nil {
					t.Fatal(err)
				}
				if err := codegen.ValidateCalls(calls, owners[name]); err != nil {
					t.Fatal(err)
				}
			}
			calls, err := codegen.Calls(ctx, artifacts["negative_archive"], goTool, []string{handlerSymbol})
			report["negativeCalls"] = calls
			if err != nil {
				t.Fatal(err)
			}
			var wrapper *codegen.WrapperCall
			if err := codegen.ValidateCalls(calls, []string{handlerSymbol}); !errors.As(err, &wrapper) || !strings.Contains(wrapper.Target, "machineAtomicPtr).Load") {
				t.Fatalf("real no-inline control did not reject the expected Load: %v", err)
			}
			report["positiveInlineAndCallsPassed"] = true
			report["realNoInlineControlRejected"] = true
		})
	}
}
