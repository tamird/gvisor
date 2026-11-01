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

package container

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	specs "github.com/opencontainers/runtime-spec/specs-go"
	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/sentry/control/controlapi"
	"gvisor.dev/gvisor/pkg/state/statefile"
	"gvisor.dev/gvisor/pkg/test/testutil"
	"gvisor.dev/gvisor/runsc/config"
	"gvisor.dev/gvisor/runsc/sandbox"
)

func gcSaveMemory(pid int) (rss, hwm uint64, err error) {
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/status", pid))
	if err != nil {
		return 0, 0, err
	}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) != 3 {
			continue
		}
		switch fields[0] {
		case "VmRSS:", "VmHWM:":
			if fields[2] != "kB" {
				return 0, 0, fmt.Errorf("memory units: %s", line)
			}
			value, err := strconv.ParseUint(fields[1], 10, 64)
			if err != nil {
				return 0, 0, err
			}
			if fields[0] == "VmRSS:" {
				rss = value * 1024
			} else {
				hwm = value * 1024
			}
		}
	}
	if hwm == 0 {
		return 0, 0, io.EOF
	}
	return rss, hwm, nil
}

// TestGCReferenceSave reuses the native tmpfs lifecycle fixture. It measures a
// non-resuming save, then independently restores and verifies its archive.
func TestGCReferenceSave(t *testing.T) {
	const memoryLimit int64 = 2 << 30
	spec, conf := sleepSpecConf(t)
	conf.Overlay2.Set("none")
	conf.Debug, conf.Strace, conf.LogPackets = false, false, false
	conf.Platform = "systrap"
	cgPath := "/" + testutil.RandomID("gvisor-gc-save")
	memLimit := memoryLimit
	spec.Mounts = []specs.Mount{{Type: "tmpfs", Destination: "/tmpfs"}}
	spec.Linux = &specs.Linux{CgroupsPath: cgPath, Resources: &specs.LinuxResources{Memory: &specs.LinuxMemory{Limit: &memLimit}}}
	_, bundle, cleanup, err := testutil.SetupContainer(spec, conf)
	if err != nil {
		t.Fatal(err)
	}
	defer cleanup()
	args := Args{ID: testutil.RandomContainerID(), Spec: spec, BundleDir: bundle}
	cont, err := New(conf, args)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if cont != nil {
			cont.Destroy()
		}
	}()
	if err := cont.Start(conf); err != nil {
		t.Fatal(err)
	}
	testApp, err := testutil.FindFile("test/cmd/test_app/test_app")
	if err != nil {
		t.Fatal(err)
	}
	work := func(c *Container, command string) {
		t.Helper()
		f, err := os.Open(testApp)
		if err != nil {
			t.Fatal(err)
		}
		defer f.Close()
		status, err := c.executeSync(conf, &controlapi.ExecArgs{
			Argv:        []string{"test_app", command, "--target-dir=/tmpfs", "--depth=100", "--file-per-level=1000", "--file-size=0"},
			FilePayload: controlapi.NewFilePayload(map[int]*os.File{0: os.Stdin, 1: os.Stdout, 2: os.Stderr}, f),
		})
		if err != nil || !status.Exited() || status.ExitStatus() != 0 {
			t.Fatalf("%s: %v, %v", command, status, err)
		}
	}
	work(cont, "fsTreeCreate")
	work(cont, "fsTreeVerify")
	if err := waitForProcessCount(cont, 1); err != nil {
		t.Fatal(err)
	}
	// All three arms use the existing idle reclamation. This exceeds both
	// startup deadlines without injecting an extra collection into any arm.
	time.Sleep(2 * time.Second)
	pid := cont.SandboxPid()
	processStatus, err := os.ReadFile(fmt.Sprintf("/proc/%d/status", pid))
	if err != nil {
		t.Fatal(err)
	}
	parentPID := ""
	allowedCPUs := ""
	for _, line := range strings.Split(string(processStatus), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 2 {
			if fields[0] == "PPid:" {
				parentPID = fields[1]
			}
			if fields[0] == "Cpus_allowed_list:" {
				allowedCPUs = fields[1]
			}
		}
	}
	if parentPID != strconv.Itoa(os.Getpid()) || allowedCPUs == "" {
		t.Fatalf("Sentry is not a verified direct child: parent=%q want=%d cpus=%q", parentPID, os.Getpid(), allowedCPUs)
	}
	membership, err := os.ReadFile(fmt.Sprintf("/proc/%d/cgroup", pid))
	if err != nil || !strings.Contains("\n"+string(membership), "\n0::"+cgPath+"\n") {
		t.Fatalf("Sentry cgroup membership: %q, %v", membership, err)
	}
	cgDir := filepath.Join("/sys/fs/cgroup", cgPath)
	limit, err := os.ReadFile(filepath.Join(cgDir, "memory.max"))
	if err != nil || strings.TrimSpace(string(limit)) != strconv.FormatInt(memoryLimit, 10) {
		t.Fatalf("memory limit: %q, %v", limit, err)
	}
	var quotas []string
	for dir := cgDir; ; dir = filepath.Dir(dir) {
		value, err := os.ReadFile(filepath.Join(dir, "cpu.max"))
		// cpu.max exists only on non-root cgroups. A delegated mount root
		// can expose it, so omit only an absent file at this mount root.
		if dir == "/sys/fs/cgroup" && os.IsNotExist(err) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		quota := strings.TrimSpace(string(value))
		fields := strings.Fields(quota)
		if len(fields) != 2 {
			t.Fatalf("cpu.max: %q", quota)
		}
		period, err := strconv.ParseUint(fields[1], 10, 64)
		if err != nil || period == 0 {
			t.Fatalf("cpu.max period: %q", quota)
		}
		if fields[0] != "max" {
			limit, err := strconv.ParseUint(fields[0], 10, 64)
			if err != nil || limit == 0 {
				t.Fatalf("cpu.max limit: %q", quota)
			}
		}
		quotas = append(quotas, quota)
		if dir == "/sys/fs/cgroup" {
			break
		}
	}
	if !strings.HasPrefix(quotas[0], "max ") {
		t.Fatalf("unexpected sandbox quota: %v", quotas)
	}
	command, err := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid))
	if err != nil {
		t.Fatal(err)
	}
	boot := make(map[string]string)
	for _, arg := range strings.Split(string(command), "\x00") {
		key, value, ok := strings.Cut(arg, "=")
		if ok && (key == "--total-memory" || key == "--cpu-num" || key == "--lifecycle-cpu-num") {
			boot[key] = value
		}
	}
	if boot["--total-memory"] != strconv.FormatInt(memoryLimit, 10) {
		t.Fatalf("boot memory: %v", boot)
	}
	beforeRSS, beforeHWM, err := gcSaveMemory(pid)
	if err != nil {
		t.Fatal(err)
	}
	imageDir := t.TempDir()
	stop := make(chan struct{})
	done := make(chan struct{})
	var peak, samples uint64
	var maxGap time.Duration
	start := time.Now()
	go func() {
		defer close(done)
		ticker := time.NewTicker(time.Millisecond)
		defer ticker.Stop()
		previous := start
		for {
			select {
			case <-stop:
				maxGap = max(maxGap, time.Since(previous))
				return
			case <-ticker.C:
				now := time.Now()
				if rss, _, err := gcSaveMemory(pid); err == nil {
					peak = max(peak, rss)
					samples++
					maxGap = max(maxGap, now.Sub(previous))
					previous = now
				}
			}
		}
	}()
	defer func() {
		select {
		case <-done:
		default:
			close(stop)
			<-done
		}
	}()
	engine := config.StateEngineWire
	if err := cont.Checkpoint(conf, imageDir, sandbox.CheckpointOpts{Compression: statefile.CompressionLevelNone, StateEngine: &engine}); err != nil {
		t.Fatal(err)
	}
	returned := time.Now()
	var info unix.Siginfo
	var usage unix.Rusage
	for {
		err := unix.Waitid(unix.P_PID, pid, &info, unix.WEXITED|unix.WNOWAIT, &usage)
		if err == unix.EINTR {
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		break
	}
	exited := time.Now()
	close(stop)
	<-done
	if samples == 0 || usage.Maxrss <= 0 {
		t.Fatalf("missing memory observations: %d, %d", samples, usage.Maxrss)
	}
	row := map[string]any{
		"memory_limit":        memoryLimit,
		"sentry_allowed_cpus": allowedCPUs, "direct_child": true,
		"cpu_quotas": quotas, "coordinator_cpus": runtime.NumCPU(), "boot": boot,
		"before_rss": beforeRSS, "before_hwm": beforeHWM,
		"lifetime_maxrss": uint64(usage.Maxrss) * 1024, "observed_save_exit_rss": peak,
		"samples": samples, "max_sample_gap_ns": maxGap.Nanoseconds(),
		"checkpoint_ns": returned.Sub(start).Nanoseconds(), "checkpoint_exit_ns": exited.Sub(start).Nanoseconds(),
	}
	measured, err := json.Marshal(row)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("GC-SAVE-MEASURED %s", measured)
	status, err := cont.Wait()
	if err != nil || !status.Exited() || status.ExitStatus() != 0 {
		t.Fatalf("save exit: %v, %v", status, err)
	}
	if err := cont.Destroy(); err != nil {
		t.Fatal(err)
	}
	cont = nil
	files := make(map[string]any)
	for _, name := range []string{"checkpoint.img", "pages_meta.img", "pages.img"} {
		f, err := os.Open(filepath.Join(imageDir, name))
		if err != nil {
			t.Fatal(err)
		}
		h := sha256.New()
		size, err := io.Copy(h, f)
		f.Close()
		if err != nil || size <= 0 {
			t.Fatalf("archive %s: %d, %v", name, size, err)
		}
		files[name] = map[string]any{"sha256": fmt.Sprintf("%x", h.Sum(nil)), "bytes": size}
	}
	restored, err := New(conf, args)
	if err != nil {
		t.Fatal(err)
	}
	defer restored.Destroy()
	if err := restored.Restore(conf, imageDir, sandbox.DirectIOOff, false, nil); err != nil {
		t.Fatal(err)
	}
	work(restored, "fsTreeVerify")
	row["files_verified"] = 100000
	row["archive"] = files
	data, err := json.Marshal(row)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("GC-SAVE-ROW %s", data)
}
