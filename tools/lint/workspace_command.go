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

// workspace_command runs a Bazel tool on the writable checkout. Bazel starts
// runnable targets in their runfiles, which may be separate from the sources.
package main

import (
	"errors"
	"log"
	"os"
	"os/exec"

	"github.com/bazelbuild/rules_go/go/runfiles"
)

func main() {
	if len(os.Args) < 2 {
		log.Fatal("missing executable runfile")
	}
	tool, err := runfiles.Rlocation(os.Args[1])
	if err != nil {
		log.Fatal(err)
	}
	workspace, ok := os.LookupEnv("BUILD_WORKSPACE_DIRECTORY")
	if !ok {
		log.Fatal("run this target with bazel run")
	}
	cmd := exec.Command(tool, os.Args[2:]...)
	cmd.Dir = workspace
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			os.Exit(exit.ExitCode())
		}
		log.Fatal(err)
	}
}
