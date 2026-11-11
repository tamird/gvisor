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

package prewarmer_test

import (
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/testutil"
)

func readFDSize(t *testing.T) int {
	t.Helper()
	b, err := os.ReadFile("/proc/self/status")
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range strings.Split(string(b), "\n") {
		if value, ok := strings.CutPrefix(line, "FDSize:"); ok {
			size, err := strconv.Atoi(strings.TrimSpace(value))
			if err != nil {
				t.Fatal(err)
			}
			return size
		}
	}
	t.Fatal("missing FDSize in /proc/self/status")
	return 0
}

func TestFDSize(t *testing.T) {
	parent := os.Getenv("FDSIZE_PARENT")
	if parent == "" {
		for _, mode := range []string{"cold", "closed", "held", "inherited"} {
			for i := 0; i < 3; i++ {
				cmd := exec.Command(os.Args[0], "-test.run=^TestFDSize$", "-test.v")
				cmd.Env = append(os.Environ(), "FDSIZE_PARENT="+mode)
				b, err := cmd.CombinedOutput()
				t.Logf("%s", b)
				if err != nil {
					t.Fatal(err)
				}
			}
		}
		return
	}

	initial := readFDSize(t)
	if initial >= 256 {
		t.Fatalf("unusable cold control: parent initially has %d slots", initial)
	}
	if parent != "cold" {
		// Grow the real parent table. Separately test a high descriptor closed
		// before fork, closed on exec, and deliberately inherited across exec.
		op := uintptr(syscall.F_DUPFD_CLOEXEC)
		if parent == "inherited" {
			op = syscall.F_DUPFD
		}
		fd, _, errno := syscall.Syscall(syscall.SYS_FCNTL, 0, op, 511)
		if errno != 0 {
			t.Fatal(errno)
		}
		if parent == "closed" {
			if err := syscall.Close(int(fd)); err != nil {
				t.Fatal(err)
			}
		} else {
			defer syscall.Close(int(fd))
		}
	}
	parentSlots := readFDSize(t)
	if parent != "cold" && parentSlots < 512 {
		t.Fatalf("parent table did not expand: %d", parentSlots)
	}

	expander, err := testutil.FindFile("runsc/prewarmer/fdtable_expander")
	if err != nil {
		t.Fatal(err)
	}
	launcher, err := testutil.FindFile("runsc/prewarmer/gvisor-sentry-prewarmer")
	if err != nil {
		t.Fatal(err)
	}
	f, err := os.Open(expander)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	records := regexp.MustCompile("(?m)^QA (before|after) FDSize:[[:blank:]]*([0-9]+)$")

	for _, path := range []string{"direct", "launcher"} {
		cmd := exec.Command(expander)
		if path == "launcher" {
			cmd = exec.Command(launcher)
			cmd.Args[0] = "fdtable_expander"
			cmd.ExtraFiles = []*os.File{f}
		}
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("%s: %v: %s", path, err, out)
		}
		lines := strings.Split(strings.TrimSpace(string(out)), "\n")
		m := records.FindAllStringSubmatch(string(out), -1)
		if len(lines) != 3 || len(m) != 2 || m[0][1] != "before" || m[1][1] != "after" {
			t.Fatalf("unexpected helper output: %s", out)
		}
		latency, err := time.ParseDuration(lines[1])
		if err != nil || latency <= 0 {
			t.Fatalf("invalid dup3 timing: %q", lines[1])
		}
		first, _ := strconv.Atoi(m[0][2])
		last, _ := strconv.Atoi(m[1][2])
		if last < 256 || last < first {
			t.Fatalf("dup3 did not reach target 255: %d -> %d", first, last)
		}
		fmt.Printf("QA_CAPACITY parent=%s initial=%d parentSlots=%d path=%s before=%d after=%d dup3=%v\n",
			parent, initial, parentSlots, path, first, last, latency)
	}
}
