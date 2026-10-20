// Copyright 2019 The gVisor Authors.
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

package lib

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
)

var nodejsTestRegEx = regexp.MustCompile(`^test-[^-].+\.js$`)

// nodejsRunner implements TestRunner for NodeJS.
type nodejsRunner struct{}

var _ TestRunner = nodejsRunner{}

// ListTests implements TestRunner.ListTests.
func (nodejsRunner) ListTests() ([]string, error) {
	cmd := nodejsCommand("--list-tests")
	cmd.Stderr = os.Stderr
	output, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("listing Node tests: %w", err)
	}
	var tests []string
	if err := json.Unmarshal(output, &tests); err != nil {
		return nil, fmt.Errorf("decoding Node test inventory: %w", err)
	}
	// Keep the existing filename scope, but let Node's suite configuration
	// determine which files are tests rather than recursively finding fixtures.
	return slices.DeleteFunc(tests, func(name string) bool {
		return !nodejsTestRegEx.MatchString(filepath.Base(name))
	}), nil
}

// TestCmds implements TestRunner.TestCmds.
func (nodejsRunner) TestCmds(tests []string) []*exec.Cmd {
	return []*exec.Cmd{nodejsCommand(tests...)}
}

func nodejsCommand(tests ...string) *exec.Cmd {
	args := append([]string{filepath.Join("tools", "test.py"), "--timeout=180", "--progress=tap"}, tests...)
	return exec.Command("/usr/bin/python3.10", args...)
}
