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

// Binary runner runs a command inside the shared Docker test fixture.
package main

import (
	"errors"
	"flag"
	"log"
	"os"
	"os/exec"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
)

func main() {
	flag.Parse()
	if flag.NArg() == 0 {
		log.Fatal("a test command is required after --")
	}
	os.Exit(dockerutil.RunTests(func() int {
		cmd := exec.Command(flag.Arg(0), flag.Args()[1:]...)
		cmd.Env = append(os.Environ(), "RUNTIME="+dockerutil.Runtime())
		cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
		if err := cmd.Run(); err != nil {
			log.Printf("test command failed: %v", err)
			var exitErr *exec.ExitError
			if errors.As(err, &exitErr) && exitErr.ExitCode() > 0 {
				return exitErr.ExitCode()
			}
			return 1
		}
		return 0
	}))
}
