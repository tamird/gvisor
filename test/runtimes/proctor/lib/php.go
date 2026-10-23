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
	"os/exec"
	"regexp"
)

var phpTestRegEx = regexp.MustCompile(`^.+\.phpt$`)

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
func (phpRunner) TestCmds(tests []string) []*exec.Cmd {
	// Match the runner settings used by PHP's Makefile, while testing the
	// installed interpreter instead of rebuilding it with make test.
	args := []string{
		"-n",
		"-d", "open_basedir=",
		"-d", "output_buffering=0",
		"-d", "memory_limit=-1",
		"run-tests.php",
		"-n",
		"-p", "/usr/local/bin/php",
	}
	return []*exec.Cmd{exec.Command("/usr/local/bin/php", append(args, tests...)...)}
}
