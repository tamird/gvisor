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

package harness

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/google/pprof/profile"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

var (
	pgoProfiletool = flag.String("pgo-profiletool", "", "declared profiletool for PGO source-output qualification")
	pgoPlatform    = flag.String("pgo-platform", "", "platform identifying the committed PGO baseline")
	pgoBenchmark   = flag.String("pgo-benchmark", "", "benchmark identifying the committed PGO baseline")
)

// preparePGO uses the public Make profile arguments with action-owned outputs.
// It never modifies runsc/profiles/data or applies the publication-age policy.
func preparePGO() ([]string, func() error, error) {
	if !dockerutil.IsPGO() || (*pgoPlatform != "systrap" && *pgoPlatform != "kvm") || *pgoBenchmark == "" {
		return nil, nil, errors.New("PGO collection requires PGO benchmarks, a platform and a benchmark name")
	}
	output := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if output == "" {
		return nil, nil, errors.New("PGO collection requires TEST_UNDECLARED_OUTPUTS_DIR")
	}
	directory, err := filepath.Abs(filepath.Join(output, "pgo"))
	if err != nil {
		return nil, nil, err
	}
	temporary := os.Getenv("TEST_TMPDIR")
	if temporary == "" {
		return nil, nil, errors.New("PGO collection requires TEST_TMPDIR")
	}
	raw, err := filepath.Abs(filepath.Join(temporary, "pgo-raw"))
	if err != nil {
		return nil, nil, err
	}
	if err := os.MkdirAll(directory, 0755); err != nil {
		return nil, nil, err
	}
	if err := os.MkdirAll(raw, 0755); err != nil {
		return nil, nil, err
	}
	args := []string{
		"--profile",
		"--profile-gc-interval=3s",
		"--profile-cpu=" + filepath.Join(raw, *pgoBenchmark+".%YYYY%-%MM%-%DD%_%HH%-%II%-%SS%-%NN%.pgo.pprof.pb.gz"),
	}
	return args, func() error { return finishPGO(directory, raw) }, nil
}

func finishPGO(directory, raw string) error {
	tool := func(args ...string) ([]byte, error) {
		output, err := exec.Command(*pgoProfiletool, args...).CombinedOutput()
		if err != nil {
			return output, fmt.Errorf("profiletool %v: %w: %s", args, err, output)
		}
		return output, nil
	}
	keyOutput, err := tool("runtime-info")
	if err != nil {
		return err
	}
	key := strings.TrimSpace(string(keyOutput))
	merged := filepath.Join(directory, *pgoBenchmark+".pgo.pprof.pb.gz")
	if _, err := tool("merge", "--out="+merged, raw); err != nil {
		return err
	}
	data, err := os.ReadFile(merged)
	if err != nil {
		return err
	}
	prof, err := profile.ParseData(data)
	if err != nil {
		return err
	}
	if err := prof.CheckValid(); err != nil {
		return err
	}
	var cpuSamples int64
	for column, typ := range prof.SampleType {
		if typ.Type == "cpu" {
			for _, sample := range prof.Sample {
				cpuSamples += sample.Value[column]
			}
		}
	}
	if cpuSamples <= 0 {
		return errors.New("generated profile has no positive CPU samples")
	}
	baselines, err := testutil.FindFile("runsc/profiles/data")
	if err != nil {
		return err
	}
	baseline := filepath.Join(baselines, key+"_"+*pgoPlatform, filepath.Base(merged))
	baselineData, err := os.ReadFile(baseline)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	decision := "add"
	var baselineSHA string
	var comparison []byte
	if err == nil {
		baselineSHA = fmt.Sprintf("%x", sha256.Sum256(baselineData))
		// Validate the real old/new comparison separately: profiletool uses
		// exit 1 both for invalid data and for normal profile drift.
		if _, err := tool("check-similar", "--threshold=0", baseline, merged); err != nil {
			return err
		}
		comparison, err = tool("check-similar", "--threshold=0.7", baseline, merged)
		decision = "retain"
		if err != nil {
			if exit, ok := errors.AsType[*exec.ExitError](err); !ok || exit.ExitCode() != 1 {
				return err
			}
			decision = "refresh"
		}
	}
	metadata := struct {
		RuntimeKey      string `json:"runtime_key"`
		Platform        string `json:"platform"`
		Benchmark       string `json:"benchmark"`
		GeneratedSHA256 string `json:"generated_sha256"`
		BaselineSHA256  string `json:"baseline_sha256,omitempty"`
		CPUSamples      int64  `json:"cpu_samples"`
		Decision        string `json:"decision"`
		Comparison      string `json:"comparison"`
	}{key, *pgoPlatform, *pgoBenchmark, fmt.Sprintf("%x", sha256.Sum256(data)), baselineSHA, cpuSamples, decision, string(comparison)}
	encoded, err := json.MarshalIndent(metadata, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(directory, "result.json"), append(encoded, '\n'), 0644)
}
