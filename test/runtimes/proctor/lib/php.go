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
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strings"
)

var phpTestRegEx = regexp.MustCompile(`^.+\.phpt$`)

var phpCaptureStdioRegEx = regexp.MustCompile(`(?m)^--CAPTURE_STDIO--`)

// phpRunner implements TestRunner for PHP.
type phpRunner struct{}

var _ TestRunner = phpRunner{}

// ListTests implements TestRunner.ListTests.
func (phpRunner) ListTests() ([]string, error) {
	testSlice, err := Search(".", phpTestRegEx)
	if err != nil {
		return nil, err
	}
	return testSlice, nil
}

// TestCmds implements TestRunner.TestCmds.
func (phpRunner) TestCmds(tests []string) ([]*exec.Cmd, error) {
	var cmds []*exec.Cmd
	var batch []string
	terminal := false
	for _, test := range tests {
		data, err := os.ReadFile(test)
		if err != nil {
			return nil, fmt.Errorf("reading PHP test %q: %w", test, err)
		}
		// run-tests.php requires all three inherited streams to be terminals
		// before running CAPTURE_STDIO tests, even if they capture every stream.
		needsTerminal := phpCaptureStdioRegEx.Match(data)
		if len(batch) > 0 && needsTerminal != terminal {
			cmds = append(cmds, phpCommand(batch, terminal))
			batch = nil
		}
		batch = append(batch, test)
		terminal = needsTerminal
	}
	if len(batch) > 0 {
		cmds = append(cmds, phpCommand(batch, terminal))
	}
	return cmds, nil
}

func phpCommand(tests []string, terminal bool) *exec.Cmd {
	selection := "--show-diff " + strings.Join(tests, " ")
	if !terminal {
		return exec.Command("make", "test", "TESTS="+selection)
	}
	// Keep Docker's ordinary output stream; script provides the terminal only
	// inside the container. --return preserves the test command's exit status.
	cmd := exec.Command("script", "--quiet", "--return", "--command", `exec make test "TESTS=$TESTS"`, "/dev/null")
	cmd.Env = append(cmd.Environ(), "TESTS=-q "+selection)
	return cmd
}
