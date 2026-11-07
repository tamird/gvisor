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

package cmd

import (
	"bytes"
	"debug/elf"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/testutil"
)

func TestWorkerTsyncReporting(t *testing.T) {
	binary, err := testutil.FindFile("test/syscalls/linux/seccomp_test")
	if err != nil {
		t.Fatal(err)
	}
	image, err := elf.Open(binary)
	if err != nil {
		t.Fatal(err)
	}
	machine := uint16(image.Machine)
	image.Close()
	release, err := os.ReadFile("/proc/sys/kernel/osrelease")
	if err != nil {
		t.Fatal(err)
	}
	osrelease := strings.TrimSpace(string(release))
	if !regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._+-]{0,95}$`).MatchString(osrelease) {
		t.Fatal("unexpected kernel release")
	}
	info, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		t.Fatal(err)
	}
	field := map[string]string{"arm64": "CPU implementer", "amd64": "vendor_id"}[runtime.GOARCH]
	native := false
	for _, line := range strings.Split(string(info), "\n") {
		k, _, ok := strings.Cut(line, ":")
		if ok && strings.TrimSpace(k) == field {
			native = true
			break
		}
	}
	t.Logf("TSYNC_WORKER cpu=%s machine=%d uid=%d native=%t release=%s", runtime.GOARCH, machine, os.Getuid(), native, osrelease)
	if !native || os.Getuid() != 0 {
		t.Fatal("unexpected worker")
	}
	cmd := exec.CommandContext(t.Context(), binary, "--gtest_filter=SeccompTest.TsyncAppliesToAllThreads")
	cmd.Env = []string{"PATH=/usr/bin:/bin", "LANG=C", "GTEST_COLOR=no"}
	var output bytes.Buffer
	cmd.Stdout, cmd.Stderr = &output, &output
	err = testutil.RunCmd(cmd)
	text := output.String()
	// Relay only the isolated test's aggregate witness, never the raw child log.
	marker := regexp.MustCompile(`(?m)^TSYNC_CONTROL peer_delta=(-?[0-9]{1,5}) installer_delta=(-?[0-9]{1,5}) same_before=([01]) counts=([01]) peer_baseline=(-?1|0) installer_baseline=(-?1|0) peer_enforced=(-?1|0) installer_enforced=(-?1|0)\r?$`).FindAllString(text, -1)
	for _, m := range marker {
		t.Log(m)
	}
	if err != nil || len(marker) != 1 || !strings.Contains(text, "[       OK ] SeccompTest.TsyncAppliesToAllThreads") {
		t.Fatalf("isolated GTest exit=%v markers=%d", err, len(marker))
	}
}

func threadFilterCount(path string) (int, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	for _, line := range strings.Split(string(data), "\n") {
		if raw, ok := strings.CutPrefix(line, "Seccomp_filters:"); ok {
			n, err := strconv.Atoi(strings.TrimSpace(raw))
			if err != nil || n < 0 || n > 10000 {
				return 0, fmt.Errorf("invalid filter count")
			}
			return n, nil
		}
	}
	return 0, fmt.Errorf("no displayed filter count")
}

// The thread's start time distinguishes a stable observation from a reused TID.
// The collector keeps both numbers private.
func taskStart(dir string) (uint64, error) {
	b, err := os.ReadFile(filepath.Join(dir, "stat"))
	if err != nil {
		return 0, err
	}
	last := bytes.LastIndexByte(b, ')')
	if last < 0 {
		return 0, fmt.Errorf("invalid task stat")
	}
	fields := strings.Fields(string(b[last+1:]))
	if len(fields) < 20 {
		return 0, fmt.Errorf("short task stat")
	}
	return strconv.ParseUint(fields[19], 10, 64)
}

type observedThread struct {
	name  string
	start uint64
}

func filterSnapshot(pid int) (map[observedThread]int, bool, error) {
	base := filepath.Join("/proc", strconv.Itoa(pid))
	leader, err := threadFilterCount(filepath.Join(base, "status"))
	if err != nil {
		return nil, false, err
	}
	tasks, err := os.ReadDir(filepath.Join(base, "task"))
	if err != nil {
		return nil, false, err
	}
	observed := make(map[observedThread]int)
	for _, task := range tasks {
		dir := filepath.Join(base, "task", task.Name())
		start, err := taskStart(dir)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, false, err
		}
		value, err := threadFilterCount(filepath.Join(dir, "status"))
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, false, err
		}
		end, err := taskStart(dir)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, false, err
		}
		if start == end {
			observed[observedThread{task.Name(), start}] = value - leader
		}
	}
	after, err := threadFilterCount(filepath.Join(base, "status"))
	return observed, after == leader && len(observed) > 0, err
}

func observeSentryCounts(t *testing.T, pid int) {
	t.Helper()
	var snaps [3]map[observedThread]int
	for i := range snaps {
		if i != 0 {
			time.Sleep(100 * time.Millisecond)
		}
		values, stable, err := filterSnapshot(pid)
		if err != nil || !stable {
			t.Logf("TSYNC_SAMPLE %s index=%d incomplete", t.Name(), i)
			return
		}
		snaps[i] = values
		above, below := 0, 0
		for _, d := range values {
			if d > 0 {
				above++
			} else if d < 0 {
				below++
			}
		}
		t.Logf("TSYNC_SAMPLE %s index=%d tasks=%d above=%d below=%d", t.Name(), i, len(values), above, below)
	}
	shared, persistent, cleared := 0, 0, 0
	for id, first := range snaps[0] {
		middle, ok1 := snaps[1][id]
		last, ok2 := snaps[2][id]
		if !ok1 || !ok2 {
			continue
		}
		shared++
		if first != 0 && middle == first && last == first {
			persistent++
		}
		if first != 0 && last == 0 {
			cleared++
		}
	}
	t.Logf("TSYNC_STABILITY %s shared=%d persistent=%d cleared=%d", t.Name(), shared, persistent, cleared)
}
