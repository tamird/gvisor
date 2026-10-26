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
)

var phpTestRegEx = regexp.MustCompile(`^.+\.phpt$`)

var phpCaptureStdioRegEx = regexp.MustCompile(`(?m)^--CAPTURE_STDIO--`)
var phpRedirectRegEx = regexp.MustCompile(`(?m)^--REDIRECTTEST--`)

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
		redirect := phpRedirectRegEx.Match(data)
		if len(batch) > 0 && (needsTerminal != terminal || redirect) {
			cmds = append(cmds, phpCommand(batch, terminal, false))
			batch = nil
		}
		// Native JUnit keys redirected children by their remapped filename.
		// Isolate the parent so another invocation cannot overwrite that identity.
		if redirect {
			cmds = append(cmds, phpCommand([]string{test}, needsTerminal, true))
			continue
		}
		batch = append(batch, test)
		terminal = needsTerminal
	}
	if len(batch) > 0 {
		cmds = append(cmds, phpCommand(batch, terminal, false))
	}
	return cmds, nil
}

// Allocate report files when the command executes, since proctor can run the
// same command repeatedly and PHP truncates both native output files on startup.
// The path marker binds each execution to its retained native files; test states
// come from PHP's -W output, never its formatted console output.
const phpCommandScript = `set -eu
TESTS="--show-diff $*"
if [ -d /proctor-artifacts ]; then
    php_report=$(mktemp -d /proctor-artifacts/php-results.XXXXXXXX)
    printf '%s\n' "$@" > "$php_report/selection.txt"
    pwd -P > "$php_report/root.txt"
    printf '%s\n' "$PHP_TEST_REDIRECT" > "$php_report/redirect.txt"
    export TEST_PHP_JUNIT="$php_report/junit.xml"
    TESTS="-W $php_report/results.tsv $TESTS"
    printf 'gvisor-php-report %s\n' "$php_report"
fi
export TESTS
if [ "$PHP_TEST_TERMINAL" = true ]; then
    TESTS="-q $TESTS"
    exec script --quiet --return --command 'exec make test "TESTS=$TESTS"' /dev/null
fi
exec make test "TESTS=$TESTS"
`

func phpCommand(tests []string, terminal, redirect bool) *exec.Cmd {
	args := append([]string{"-c", phpCommandScript, "php-test"}, tests...)
	cmd := exec.Command("sh", args...)
	cmd.Env = append(cmd.Environ(), fmt.Sprintf("PHP_TEST_TERMINAL=%t", terminal), fmt.Sprintf("PHP_TEST_REDIRECT=%t", redirect))
	return cmd
}
