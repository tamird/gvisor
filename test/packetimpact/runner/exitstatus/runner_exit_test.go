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

package exitstatus_test

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"gvisor.dev/gvisor/pkg/test/testutil"
)

const childMode = "PACKETIMPACT_TESTBENCH_EXIT_MODE"
const started = "entered the testbench"

func TestMain(m *testing.M) {
	// PacketImpact supplies --dut_infos_json when it starts its testbench.
	if mode := os.Getenv(childMode); mode != "" && len(os.Args) > 1 && os.Args[1] == "--dut_infos_json" {
		fmt.Println(started)
		switch mode {
		case "pass":
			os.Exit(0)
		case "fail":
			os.Exit(7)
		case "signal":
			if err := syscall.Kill(os.Getpid(), syscall.SIGKILL); err != nil {
				fmt.Fprintln(os.Stderr, err)
			}
		}
		os.Exit(99)
	}
	os.Exit(m.Run())
}

func TestRunnerExit(t *testing.T) {
	runner, err := testutil.FindFile("test/packetimpact/runner/main")
	if err != nil {
		t.Fatal(err)
	}
	child, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name       string
		mode       string
		expected   bool
		wantCode   int
		diagnostic string
	}{
		{"OrdinaryPass", "pass", false, 0, ""},
		{"UnexpectedPass", "pass", true, 1, "expected to fail"},
		{"OrdinaryFailure", "fail", false, 7, ""},
		{"ExpectedFailure", "fail", true, 0, ""},
		{"MissingTestbench", "missing", true, 1, "no such file or directory"},
		{"SignaledTestbench", "signal", true, 1, "signal: killed"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			bench := child
			if tc.mode == "missing" {
				bench = filepath.Join(t.TempDir(), "missing-testbench")
			}
			cmd := exec.Command(runner, "--num_duts=0", "--testbench_binary="+bench,
				fmt.Sprintf("--expect_failure=%t", tc.expected))
			cmd.Env = append(os.Environ(), childMode+"="+tc.mode)
			out, err := cmd.CombinedOutput()
			code := 0
			if err != nil {
				var status *exec.ExitError
				if !errors.As(err, &status) || !status.Exited() {
					t.Fatalf("starting the PacketImpact runner: %v, output %q", err, out)
				}
				code = status.ExitCode()
			}
			got := string(out)
			t.Logf("runner exit = %d, output = %q", code, got)
			if wantStarted := tc.mode != "missing"; strings.Contains(got, started) != wantStarted {
				t.Fatalf("testbench startup marker: want %t, output %q", wantStarted, got)
			}
			if code != tc.wantCode || !strings.Contains(got, tc.diagnostic) {
				t.Errorf("runner exit = %d, output = %q, want exit %d and diagnostic %q", code, got, tc.wantCode, tc.diagnostic)
			}
		})
	}
}
