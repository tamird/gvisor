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

// workspace_command runs Bazel tools on the writable checkout. Bazel starts
// runnable targets in their runfiles, which may be separate from the sources.
package main

import (
	"encoding/json"
	"errors"
	"log"
	"os"
	"os/exec"

	"github.com/bazelbuild/rules_go/go/runfiles"
)

func main() {
	workspace, ok := os.LookupEnv("BUILD_WORKSPACE_DIRECTORY")
	if !ok {
		log.Fatal("run this target with bazel run")
	}
	// Each formatter has a runfile for the executable and one for its arguments.
	// Keeping filenames in that file also avoids the host's command-line limit
	// when Bazel starts the launcher.
	commands := os.Args[1:]
	if len(commands) == 0 || len(commands)%2 != 0 {
		log.Fatal("expected an executable and argument file for each formatter")
	}
	for len(commands) > 0 {
		if err := run(workspace, commands[0], commands[1]); err != nil {
			var exit *exec.ExitError
			if errors.As(err, &exit) {
				os.Exit(exit.ExitCode())
			}
			log.Fatal(err)
		}
		commands = commands[2:]
	}
}

func run(workspace, executable, arguments string) error {
	tool, err := runfiles.Rlocation(executable)
	if err != nil {
		return err
	}
	path, err := runfiles.Rlocation(arguments)
	if err != nil {
		return err
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var args []string
	if err := json.Unmarshal(contents, &args); err != nil {
		return err
	}
	cmd := exec.Command(tool, args...)
	cmd.Dir = workspace
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	return cmd.Run()
}
