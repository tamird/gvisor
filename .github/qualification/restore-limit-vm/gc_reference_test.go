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
	"gvisor.dev/gvisor/pkg/sentry/control/controlapi"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/statefile"
	"gvisor.dev/gvisor/pkg/state/wire"
	"gvisor.dev/gvisor/pkg/test/testutil"
	"gvisor.dev/gvisor/runsc/config"
	"gvisor.dev/gvisor/runsc/sandbox"
)

// gcGraphFrames verifies the CPUID and kernel frames after the timed save.
func gcGraphFrames(t *testing.T, image string) map[string]uint64 {
	t.Helper()
	f, err := os.Open(image)
	if err != nil {
		t.Fatal(err)
	}
	r, _, err := statefile.NewReader(f, nil)
	if err != nil {
		f.Close()
		t.Fatal(err)
	}
	defer r.Close()
	wr := wire.Reader{Reader: r}
	lengths := make(map[string]uint64)
	for _, name := range []string{"cpuid", "kernel"} {
		h, err := state.ReadHeaderMode(&wr)
		if err != nil {
			t.Fatalf("reading %s frame: %v", name, err)
		}
		if !h.Object || !h.Graph || h.Length == 0 {
			t.Fatalf("%s frame is not a nonempty graph: %+v", name, h)
		}
		if _, err := io.CopyN(io.Discard, r, int64(h.Length)); err != nil {
			t.Fatalf("reading %s payload: %v", name, err)
		}
		lengths[name] = h.Length
	}
	if err := r.Verify(); err != nil {
		t.Fatal(err)
	}
	return lengths
}

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

// TestGCReferenceRestore measures creation and restore of a fresh Sentry through
// the first successful guest read. The complete tree is verified after timing.
func TestGCReferenceRestore(t *testing.T) {
	const memoryLimit int64 = 2 << 30
	spec, conf := sleepSpecConf(t)
	conf.Overlay2.Set("none")
	conf.Debug, conf.Strace, conf.LogPackets = false, false, false
	conf.Platform = "systrap"
	const cgPath = "/benchmark"
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
	work := func(c *Container, command string, depth, files int) {
		t.Helper()
		f, err := os.Open(testApp)
		if err != nil {
			t.Fatal(err)
		}
		defer f.Close()
		status, err := c.executeSync(conf, &controlapi.ExecArgs{
			Argv:        []string{"test_app", command, "--target-dir=/tmpfs", fmt.Sprintf("--depth=%d", depth), fmt.Sprintf("--file-per-level=%d", files), "--file-size=0"},
			FilePayload: controlapi.NewFilePayload(map[int]*os.File{0: os.Stdin, 1: os.Stdout, 2: os.Stderr}, f),
		})
		if err != nil || !status.Exited() || status.ExitStatus() != 0 {
			t.Fatalf("%s: %v, %v", command, status, err)
		}
	}
	work(cont, "fsTreeCreate", 100, 1000)
	work(cont, "fsTreeVerify", 100, 1000)
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
	quotas := []string{}
	for dir := cgDir; ; dir = filepath.Dir(dir) {
		value, err := os.ReadFile(filepath.Join(dir, "cpu.max"))
		// The precreated leaf enforces one CPU; all visible ancestors are recorded.
		// The coordinator records every visible ancestor before unshare.
		if os.IsNotExist(err) {
			if dir == "/sys/fs/cgroup" {
				break
			}
			continue
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
	if len(quotas) == 0 || quotas[0] != "100000 100000" {
		t.Fatalf("unexpected sandbox quota: %v", quotas)
	}
	command, err := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid))
	if err != nil {
		t.Fatal(err)
	}
	boot := make(map[string]string)
	argv := strings.Split(string(command), "\x00")
	for i := 0; i+1 < len(argv); i++ {
		switch argv[i] {
		case "--total-memory", "--cpu-num", "--lifecycle-cpu-num":
			boot[argv[i]] = argv[i+1]
		}
	}
	if boot["--total-memory"] != strconv.FormatInt(memoryLimit, 10) {
		t.Fatalf("boot memory: %v", boot)
	}
	imageDir := t.TempDir()
	engine := config.StateEngineGraph
	if err := cont.Checkpoint(conf, imageDir, sandbox.CheckpointOpts{Compression: statefile.CompressionLevelNone, StateEngine: &engine}); err != nil {
		t.Fatal(err)
	}
	row := map[string]any{
		"memory_limit": memoryLimit, "sentry_allowed_cpus": allowedCPUs, "direct_child": true,
		"cpu_quotas": quotas, "coordinator_cpus": runtime.NumCPU(), "boot": boot,
	}
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
	row["graph_frames"] = gcGraphFrames(t, filepath.Join(imageDir, "checkpoint.img"))
	cpuStat := func() (uint64, uint64) {
		t.Helper()
		data, err := os.ReadFile(filepath.Join(cgDir, "cpu.stat"))
		if err != nil {
			t.Fatal(err)
		}
		stats := make(map[string]uint64)
		for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
			f := strings.Fields(line)
			if len(f) != 2 {
				t.Fatalf("invalid cpu.stat entry %q", line)
			}
			v, err := strconv.ParseUint(f[1], 10, 64)
			if err != nil {
				t.Fatal(err)
			}
			stats[f[0]] = v
		}
		use, haveUse := stats["usage_usec"]
		throttled, haveThrottled := stats["throttled_usec"]
		if !haveUse || !haveThrottled {
			t.Fatalf("missing counters in %q", data)
		}
		return use, throttled
	}
	cpuBefore, throttleBefore := cpuStat()
	start := time.Now()
	restored, err := New(conf, args)
	if err != nil {
		t.Fatal(err)
	}
	defer restored.Destroy()
	if err := restored.Restore(conf, imageDir, sandbox.DirectIOOff, false, nil); err != nil {
		t.Fatal(err)
	}
	returned := time.Now()
	work(restored, "fsTreeVerify", 1, 1)
	firstRead := time.Now()
	cpuAfter, throttleAfter := cpuStat()
	row["sandbox_cpu_usec"] = cpuAfter - cpuBefore
	row["sandbox_throttled_usec"] = throttleAfter - throttleBefore
	rss, hwm, err := gcSaveMemory(restored.SandboxPid())
	if err != nil {
		t.Fatal(err)
	}
	restoredQuota, err := os.ReadFile(filepath.Join(cgDir, "cpu.max"))
	if err != nil || strings.TrimSpace(string(restoredQuota)) != "100000 100000" {
		t.Fatalf("restored CPU quota: %q, %v", restoredQuota, err)
	}
	row["restore_reply_ns"] = returned.Sub(start).Nanoseconds()
	row["restore_first_read_ns"] = firstRead.Sub(start).Nanoseconds()
	row["first_read_rss"] = rss
	row["first_read_hwm"] = hwm
	work(restored, "fsTreeVerify", 100, 1000)
	restoredMembership, err := os.ReadFile(fmt.Sprintf("/proc/%d/cgroup", restored.SandboxPid()))
	if err != nil || !strings.Contains("\n"+string(restoredMembership), "\n0::"+cgPath+"\n") {
		t.Fatalf("restored Sentry cgroup: %q, %v", restoredMembership, err)
	}
	limit, err = os.ReadFile(filepath.Join(cgDir, "memory.max"))
	if err != nil || strings.TrimSpace(string(limit)) != strconv.FormatInt(memoryLimit, 10) {
		t.Fatalf("restored memory limit: %q, %v", limit, err)
	}
	events, err := os.ReadFile(filepath.Join(cgDir, "memory.events"))
	if err != nil || !strings.Contains("\n"+string(events)+"\n", "\noom_kill 0\n") {
		t.Fatalf("restored memory events: %q, %v", events, err)
	}
	row["restored_limit"] = memoryLimit
	row["oom_kills"] = 0
	row["files_verified"] = 100000
	row["archive"] = files
	data, err := json.Marshal(row)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("GC-RESTORE-ROW %s", data)
}
