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

// Binary bzlformat checks declared Starlark files or formats their workspace originals.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
)

// The repository uses its own rule macros rather than the standalone
// rules_shell, rules_python and proto_library loads.
const warnings = "-duplicated-name,-list-append,-native-py,-native-sh-binary,-native-sh-library,-native-sh-test,-native-proto"

func run(args []string) error {
	var dir, tool, paths, output, mode, lint string
	switch {
	case len(args) == 4 && args[0] == "check":
		tool, paths, output = args[1], args[2], args[3]
		dir = filepath.Join(filepath.Dir(paths), "src")
		mode, lint = "check", "warn"
	case len(args) == 3 && args[0] == "fix":
		var present bool
		dir, present = os.LookupEnv("BUILD_WORKSPACE_DIRECTORY")
		if !present {
			return errors.New("run bazel run //:buildifier_fix in the workspace")
		}
		if !filepath.IsAbs(dir) {
			return fmt.Errorf("BUILD_WORKSPACE_DIRECTORY must be absolute, got %q", dir)
		}
		// bazel run starts in the runfiles workspace. These two rootpaths
		// locate its data before Buildifier changes to the source workspace.
		tool, paths = args[1], args[2]
		mode, lint = "fix", "fix"
	default:
		return errors.New("expected check TOOL PATHS OUTPUT or fix TOOL PATHS")
	}
	binary, err := filepath.Abs(tool)
	if err != nil {
		return err
	}
	data, err := os.ReadFile(paths)
	if err != nil {
		return err
	}
	var manifest struct {
		Sources []string
		Data    []string
	}
	if err := json.Unmarshal(data, &manifest); err != nil {
		return err
	}
	if len(manifest.Sources) == 0 {
		return errors.New("no Starlark sources declared")
	}
	// Without --config, Buildifier can select BUILDIFIER_CONFIG or a file
	// above the workspace. Only use the configuration Bazel declared.
	config := "off"
	if slices.Contains(manifest.Data, ".buildifier.json") {
		config = ".buildifier.json"
	}
	// Buildifier's optional JSON diagnostic format exits zero even for lint
	// failures. Keep the text diagnostics and exit status used by Bazel.
	options := []string{"--config=" + config, "--format=", "--mode=" + mode, "--lint=" + lint, "--warnings=" + warnings, "--"}
	cmd := exec.Command(binary, append(options, manifest.Sources...)...)
	cmd.Dir, cmd.Stdout, cmd.Stderr = dir, os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		if mode == "check" {
			fmt.Fprintln(os.Stderr, "To apply Buildifier's fixes: bazel run //:buildifier_fix")
		}
		return err
	}
	if output != "" {
		return os.WriteFile(output, nil, 0644)
	}
	return nil
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			os.Exit(exit.ExitCode())
		}
		os.Exit(1)
	}
}
