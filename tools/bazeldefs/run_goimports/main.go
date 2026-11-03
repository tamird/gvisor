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

// run_goimports runs goimports with the declared Go executable on PATH.
package main

import (
	"log"
	"os"
	"os/exec"
	"path/filepath"
)

func main() {
	if len(os.Args) != 5 {
		log.Fatal("usage: run_goimports GO_BIN_DIR GOIMPORTS SRC OUT")
	}
	// goimports calls exec.LookPath("go"), which rejects executables found
	// through a relative PATH entry. Resolve the declared directory here,
	// where the Bazel execution root is the working directory.
	binDir, err := filepath.Abs(os.Args[1])
	if err != nil {
		log.Fatal(err)
	}
	out, err := os.Create(os.Args[4])
	if err != nil {
		log.Fatal(err)
	}
	cmd := exec.Command(os.Args[2], os.Args[3])
	cmd.Env = append(os.Environ(), "PATH="+binDir)
	cmd.Stdout, cmd.Stderr = out, os.Stderr
	if err := cmd.Run(); err != nil {
		if status, ok := err.(*exec.ExitError); ok {
			os.Exit(status.ExitCode())
		}
		log.Fatal(err)
	}
	if err := out.Close(); err != nil {
		log.Fatal(err)
	}
}
