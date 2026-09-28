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

package dockerutil

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"maps"
	"os/exec"
	"slices"
)

//go:embed runtime_variants.json
var runtimeVariantsJSON []byte

// RuntimeDefinition is Docker's configured runtime executable and arguments.
type RuntimeDefinition struct {
	Path string   `json:"path"`
	Args []string `json:"runtimeArgs"`
}

// RuntimeVariant names a suite configuration and its runsc arguments.
// Docker suite names are suffixes; single selected variants keep the base name.
type RuntimeVariant struct {
	Name string   `json:"name"`
	Args []string `json:"args"`
}

// RuntimeVariants returns a suite's configurations in their declared order.
func RuntimeVariants(suite string) ([]RuntimeVariant, error) {
	var suites map[string][]RuntimeVariant
	if err := json.Unmarshal(runtimeVariantsJSON, &suites); err != nil {
		return nil, fmt.Errorf("decode runtime variants: %w", err)
	}
	variants, ok := suites[suite]
	if !ok {
		return nil, fmt.Errorf("unknown runtime suite %q", suite)
	}
	return variants, nil
}

func runtimeDefinitions(runsc, name string, baseArgs []string, variants []RuntimeVariant) map[string]RuntimeDefinition {
	runtimes := make(map[string]RuntimeDefinition, len(variants))
	for _, variant := range variants {
		args := append(slices.Clone(baseArgs), "--allow-suid")
		args = append(args, variant.Args...)
		args = append(args, "--TESTONLY-test-name-env=RUNSC_TEST_NAME")
		runtimes[name+variant.Name] = RuntimeDefinition{Path: runsc, Args: args}
	}
	return runtimes
}

// InstallRuntimeVariants registers the Docker suite's runtime variants using
// runsc install. The caller owns daemon reload and the selected runtime binary.
func InstallRuntimeVariants(runsc, name, configPath string, baseArgs []string, variants []RuntimeVariant) error {
	runtimes := runtimeDefinitions(runsc, name, baseArgs, variants)
	for _, name := range slices.Sorted(maps.Keys(runtimes)) {
		runtime := runtimes[name]
		args := []string{"install", "--config_file=" + configPath, "--experimental=true", "--runtime=" + name, "--"}
		args = append(args, runtime.Args...)
		if output, err := exec.Command(runtime.Path, args...).CombinedOutput(); err != nil {
			return fmt.Errorf("install runtime %q: %w\n%s", name, err, output)
		}
	}
	return nil
}
