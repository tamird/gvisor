// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package harness

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"

	"strings"
	"testing"
	"time"

	"github.com/docker/docker/api/types/events"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/api/types/image"
	"github.com/docker/docker/client"
	"github.com/docker/docker/errdefs"
	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

var pairedLimitWorkerCPUs = flag.Bool("paired-limit-worker-cpus", false, "restrict worker threads and children to the declared CPUs before Docker startup")

var pairedResponseProbe = flag.Bool("paired-response-probe", false, "inspect Node responses without timed request samples")

var (
	pairedBase        = flag.String("paired-base-runsc", "", "declared baseline release entrypoint")
	pairedCandidate   = flag.String("paired-candidate-runsc", "", "declared candidate release entrypoint")
	pairedBaseID      = flag.String("paired-base-source", "", "baseline runtime source commit")
	pairedCandidateID = flag.String("paired-candidate-source", "", "candidate runtime source commit")
	pairedBinary      = flag.String("paired-benchmark", "", "shared declared benchmark executable")
	pairedImages      = flag.String("paired-image-archives", "", "space-separated declared image archives")
	pairedFilter      = flag.String("paired-bench", "^BenchmarkNode$", "ordinary Go benchmark selection expression")
	pairedBenchtime   = flag.String("paired-benchtime", "1000x", "ordinary Go benchmark time or operation count")
	pairedBlocks      = flag.Int("paired-blocks", 1, "number of balanced BASE/CANDIDATE/CANDIDATE/BASE blocks")
	pairedLeaves      = flag.Int("paired-leaves", 4, "number of selected benchmark leaves per visit")
	pairedRedisCPUs   = flag.String("paired-redis-cpuset", "", "explicit CPU affinity for the native Redis backend")
	pairedNodeWarmup  = flag.Int("paired-node-warmup-requests", 0, "warmup requests for each actual Node server process")
	pairedWarmups     = flag.Int("paired-warmup-pairs", 0, "unmeasured warmup visits for each runtime before balanced blocks")
	pairedObserve     = flag.Bool("paired-observe-affinity", false, "inspect actual workload affinity during capability preflight")
	pairedWorkload    = flag.String("paired-workload", "node", "workload contract: node, startup-empty, or startup")
)

func TestPairedBenchmark(t *testing.T) {
	if *pairedBlocks < 1 || *pairedLeaves < 1 || *pairedWarmups < 0 {
		t.Fatal("paired blocks and leaves must be positive")
	}
	var available unix.CPUSet
	if err := unix.SchedGetaffinity(0, &available); err != nil {
		t.Fatal(err)
	}
	for cpu := 0; cpu <= 4; cpu++ {
		if !available.IsSet(cpu) {
			t.Fatalf("required worker CPU %d is unavailable; affinity: %v", cpu, available)
		}
	}
	if (*pairedWorkload == "startup-empty" || *pairedWorkload == "startup" || *pairedLimitWorkerCPUs) && available.Count() != 5 {
		// A worker can expose additional guest CPUs beyond its reservation.
		// Re-exec before creating Docker so all new threads and children
		// inherit the declared partition.
		var partition unix.CPUSet
		for cpu := 0; cpu <= 4; cpu++ {
			partition.Set(cpu)
		}
		t.Logf("restrict worker affinity from %v to %v before re-exec", available, partition)
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
		if err := unix.SchedSetaffinity(0, &partition); err != nil {
			t.Fatal(err)
		}
		binary, err := os.Executable()
		if err != nil {
			t.Fatal(err)
		}
		t.Fatal(unix.Exec(binary, os.Args, os.Environ()))
	}

	switch *pairedWorkload {
	case "node":
	case "startup-empty", "startup":
		if *pairedResponseProbe || !*pairedObserve || available.Count() != 5 || ClientCPUSet() != "0-4" || ServerCPUSet() != "0-4" {
			t.Fatalf("startup requires affinity observation on the declared five-CPU worker with matching 0-4 partitions: available=%v count=%d response_probe=%t observe=%t client=%q server=%q", available, available.Count(), *pairedResponseProbe, *pairedObserve, ClientCPUSet(), ServerCPUSet())
		}
		if (*pairedWorkload == "startup-empty" && *pairedLeaves != 1) || (*pairedWorkload == "startup" && *pairedLeaves != 5) {
			t.Fatal("startup workload leaf count does not match its image contract")
		}
	default:
		t.Fatalf("unknown paired workload %q", *pairedWorkload)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Minute)
	defer cancel()
	binary, err := testutil.FindFile(*pairedBinary)
	if err != nil {
		t.Fatal(err)
	}
	contents, err := os.ReadFile(binary)
	if err != nil {
		t.Fatal(err)
	}
	workerID := rand.Text()
	binarySHA := fmt.Sprintf("%x", sha256.Sum256(contents))
	arms := []struct{ name, runsc, source, digest string }{
		{"BASE", *pairedBase, *pairedBaseID, ""},
		{"CANDIDATE", *pairedCandidate, *pairedCandidateID, ""},
	}
	for i := range arms {
		arm := &arms[i]
		if arm.runsc == "" || len(arm.source) != 40 {
			t.Fatal("paired benchmark requires both declared releases and source commits")
		}
		path, err := testutil.FindFile(arm.runsc)
		if err != nil {
			t.Fatal(err)
		}
		contents, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		arm.digest = fmt.Sprintf("%x", sha256.Sum256(contents))
		arm.runsc, err = filepath.Abs(path)
		if err != nil {
			t.Fatal(err)
		}
	}
	releaseProducts := make([]map[string]string, len(arms))
	allProducts := make(map[string]string)
	for i, arm := range arms {
		products, err := releaseDigests(filepath.Dir(arm.runsc))
		if err != nil {
			t.Fatal(err)
		}
		releaseProducts[i] = products
		for path, digest := range products {
			allProducts[path] = digest
		}
	}
	actualHashes := make(map[string]string)
	if err := json.NewEncoder(os.Stdout).Encode(struct {
		Products []map[string]string `json:"opaque_release_products"`
	}{releaseProducts}); err != nil {
		t.Fatal(err)
	}
	host, runtimes, config := startDockerDaemons(t, ctx, []string{arms[0].runsc, arms[1].runsc}, true)
	if *pairedLimitWorkerCPUs {
		pidBytes, err := os.ReadFile(filepath.Join(filepath.Dir(config), "docker.pid"))
		if err != nil {
			t.Fatal(err)
		}
		pid, err := strconv.Atoi(strings.TrimSpace(string(pidBytes)))
		if err != nil {
			t.Fatal(err)
		}
		if err := recordCPUResources(-1, "worker-partition", map[int]string{pid: "owned-dockerd"}, allProducts); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("DOCKER_HOST", host)
	t.Setenv("UNSANDBOXED_RUNTIME", "runc")
	cli, err := client.NewClientWithOpts(client.WithHost(host), client.WithAPIVersionNegotiation())
	if err != nil {
		t.Fatal(err)
	}
	defer cli.Close()
	for _, archive := range strings.Fields(*pairedImages) {
		loadDockerImage(t, ctx, cli, archive)
	}
	version, err := cli.ServerVersion(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if *pairedWorkload != "node" {
		t.Logf("startup Docker experimental mode: %t", version.Experimental)
	}
	images, err := cli.ImageList(ctx, image.ListOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if err := json.NewEncoder(os.Stdout).Encode(struct {
		Server   string          `json:"shared_docker_version"`
		Images   []image.Summary `json:"images"`
		Runtimes []string        `json:"registered_runtimes"`
		Releases []string        `json:"release_entrypoints"`
	}{version.Version, images, runtimes, []string{arms[0].runsc, arms[1].runsc}}); err != nil {
		t.Fatal(err)
	}
	blocks := *pairedBlocks
	warmups := *pairedWarmups
	order := make([]int, 0, 2*warmups+4*blocks)
	for range *pairedWarmups {
		order = append(order, 0, 1)
	}
	for range *pairedBlocks {
		order = append(order, 0, 1, 1, 0)
	}
	for visit, index := range order {
		arm := arms[index]
		t.Run(fmt.Sprintf("visit%d/%s", visit, arm.name), func(t *testing.T) {
			warmup := visit < 2*warmups
			block := -1
			if !warmup {
				block = (visit - 2*warmups) / 4
			}
			if err := json.NewEncoder(os.Stdout).Encode(struct {
				Visit    int    `json:"paired_visit"`
				Worker   string `json:"paired_worker"`
				Block    int    `json:"paired_block"`
				Warmup   bool   `json:"paired_warmup"`
				Arm      string `json:"arm"`
				Source   string `json:"runtime_source"`
				Process  int    `json:"pair_process"`
				RunscSHA string `json:"runsc_entrypoint_sha256"`
				NodeSHA  string `json:"benchmark_sha256"`
			}{visit, workerID, block, warmup, arm.name, arm.source, os.Getpid(), arm.digest, binarySHA}); err != nil {
				t.Fatal(err)
			}
			args := []string{
				"-test.v=" + strconv.FormatBool(*pairedResponseProbe), "--runtime=" + runtimes[index], "--config_path=" + config,
				"-test.run=^$", "-test.bench=" + *pairedFilter, "-test.benchtime=" + *pairedBenchtime,
			}
			if *pairedWorkload == "node" {
				args = append(args,
					"--node-warmup-requests="+strconv.Itoa(*pairedNodeWarmup),
					"--node-response-probe="+strconv.FormatBool(*pairedResponseProbe),
					"--client-cpuset="+ClientCPUSet(), "--server-cpuset="+ServerCPUSet(),
					"--node-redis-cpuset="+*pairedRedisCPUs, "--hey-phase-diagnostics")
			} else {
				t.Logf("startup benchmark argv: %q", args)
			}
			cmd := exec.CommandContext(ctx, binary, args...)
			var observed chan error
			var stopObserver context.CancelFunc
			if *pairedObserve {
				observeCtx, cancel := context.WithCancel(ctx)
				stopObserver = cancel
				observed = make(chan error, 1)
				messages, failures := cli.Events(observeCtx, events.ListOptions{Filters: filters.NewArgs(filters.Arg("type", "container"), filters.Arg("event", "start"))})
				go func() {
					observed <- attestBenchmarkContainers(observeCtx, cli, messages, failures, visit, releaseProducts[index], actualHashes, runtimes[index])
				}()
			}
			output, err := cmd.CombinedOutput()
			if stopObserver != nil {
				stopObserver()
				if observeErr := <-observed; observeErr != nil {
					t.Error(observeErr)
				}
			}
			remaining, processErr := ownedReleaseProcesses(allProducts)
			if processErr != nil {
				t.Error(processErr)
			}
			if encodeErr := json.NewEncoder(os.Stdout).Encode(struct {
				Visit     int            `json:"quiescent_visit"`
				Remaining map[int]string `json:"remaining_release_processes"`
			}{visit, remaining}); encodeErr != nil {
				t.Error(encodeErr)
			}
			if len(remaining) != 0 {
				t.Errorf("owned runtime processes remain after visit %d: %v", visit, remaining)
			}
			if _, writeErr := os.Stdout.Write(output); writeErr != nil {
				t.Fatal(writeErr)
			}
			if err != nil {
				t.Fatal(err)
			}
			if *pairedResponseProbe {
				return
			}
			leaves := 0
			for _, line := range strings.Split(string(output), "\n") {
				if strings.HasPrefix(line, "Benchmark") && strings.Contains(line, " ns/op") {
					leaves++
				}
			}
			if leaves != *pairedLeaves {
				t.Fatalf("completed %d benchmark leaves, want %d", leaves, *pairedLeaves)
			}
		})
		if t.Failed() {
			return
		}
	}
}

// attestBenchmarkContainers observes the actual workload containers without
// adding inspection calls to the benchmark's timed request sequence.
func attestBenchmarkContainers(ctx context.Context, cli *client.Client, messages <-chan events.Message, failures <-chan error, visit int, products, actualHashes map[string]string, selectedRuntime string) error {
	seen := make(map[string]int)
	active := make(map[int]string)
	required := map[string]int{"benchmarks/node": *pairedLeaves, "benchmarks/hey": *pairedLeaves, "benchmarks/redis": *pairedLeaves}
	if *pairedResponseProbe {
		delete(required, "benchmarks/hey")
		required["benchmarks/util"] = *pairedLeaves
	}
	switch *pairedWorkload {
	case "startup-empty":
		required = map[string]int{"benchmarks/alpine": 1}
	case "startup":
		required = map[string]int{"benchmarks/alpine": 2, "benchmarks/nginx": 2, "benchmarks/node": 1, "benchmarks/redis": 1, "benchmarks/util": 1}
	}
	for {
		select {
		case <-ctx.Done():
			for image, count := range required {
				if seen[image] < count {
					return fmt.Errorf("visit %d has no actual affinity observation for %s", visit, image)
				}
			}
			return nil
		case err, ok := <-failures:
			if !ok {
				failures = nil
				continue
			}
			if err != nil && ctx.Err() == nil {
				return fmt.Errorf("observe workload containers: %w", err)
			}
		case message, ok := <-messages:
			if !ok {
				messages = nil
				continue
			}
			image := message.Actor.Attributes["image"]
			kind := ""
			for candidate := range required {
				if strings.Contains(image, candidate) {
					kind = candidate
				}
			}
			if kind == "" {
				continue
			}
			state, err := cli.ContainerInspect(ctx, message.Actor.ID)
			// testing.B also runs a one-request calibration. Such a client may
			// exit before its start event arrives; only complete observations
			// count toward the selected leaves required for this visit.
			if errdefs.IsNotFound(err) {
				continue
			}
			if err != nil {
				return fmt.Errorf("inspect started workload %s: %w", kind, err)
			}
			if state.State == nil || !state.State.Running || state.State.Pid <= 0 {
				continue
			}
			if state.HostConfig == nil {
				return fmt.Errorf("started workload %s has no host configuration", kind)
			}
			if *pairedWorkload == "node" && kind == "benchmarks/util" && state.HostConfig.CpusetCpus == "" {
				continue
			}
			status, err := os.ReadFile("/proc/" + strconv.Itoa(state.State.Pid) + "/status")
			if os.IsNotExist(err) {
				continue
			}
			if err != nil {
				return fmt.Errorf("read workload %s affinity: %w", kind, err)
			}
			effective := ""
			for _, line := range strings.Split(string(status), "\n") {
				if value, ok := strings.CutPrefix(line, "Cpus_allowed_list:"); ok {
					effective = strings.TrimSpace(value)
				}
			}
			want := ServerCPUSet()
			if kind == "benchmarks/hey" || kind == "benchmarks/util" {
				want = ClientCPUSet()
			}
			if kind == "benchmarks/redis" {
				want = *pairedRedisCPUs
			}
			if *pairedWorkload != "node" {
				want = ServerCPUSet()
				if (state.HostConfig.CpusetCpus != "" && state.HostConfig.CpusetCpus != want) || effective != want {
					return fmt.Errorf("startup workload %s requested CPUs %q, effective %q, want %q", kind, state.HostConfig.CpusetCpus, effective, want)
				}
				wantRuntime := selectedRuntime
				if kind == "benchmarks/redis" || kind == "benchmarks/util" {
					wantRuntime = "runc"
				}
				if state.HostConfig.Runtime != wantRuntime {
					return fmt.Errorf("startup workload %s runtime %q, want %q", kind, state.HostConfig.Runtime, wantRuntime)
				}
			} else if (kind != "benchmarks/redis" || want != "") && (want == "" || state.HostConfig.CpusetCpus != want || effective != want) {
				return fmt.Errorf("workload %s requested CPUs %q, effective %q, want %q", kind, state.HostConfig.CpusetCpus, effective, want)
			}
			executable := "/proc/" + strconv.Itoa(state.State.Pid) + "/exe"
			exe, err := os.Readlink(executable)
			if os.IsNotExist(err) {
				continue
			}
			if err != nil {
				return err
			}
			digest, ok := actualHashes[exe]
			if !ok {
				digest, err = executableDigest(executable)
				if os.IsNotExist(err) {
					continue
				}
				if err != nil {
					return err
				}
				actualHashes[exe] = digest
			}
			if (kind == "benchmarks/node" || kind == "benchmarks/alpine" || kind == "benchmarks/nginx") && products[exe] != digest {
				return fmt.Errorf("actual server executable %q (%s) is outside the selected complete release", exe, digest)
			}
			if err := json.NewEncoder(os.Stdout).Encode(struct {
				Visit      int    `json:"affinity_visit"`
				Image      string `json:"image"`
				Container  string `json:"container"`
				Runtime    string `json:"runtime"`
				Requested  string `json:"requested_cpus"`
				Effective  string `json:"effective_cpus"`
				Executable string `json:"executable"`
				Digest     string `json:"executable_sha256"`
			}{visit, image, state.Name, state.HostConfig.Runtime, state.HostConfig.CpusetCpus, effective, exe, digest}); err != nil {
				return err
			}
			seen[kind]++
			active[state.State.Pid] = kind
			if kind == "benchmarks/hey" || (*pairedResponseProbe && kind == "benchmarks/util") || (*pairedWorkload != "node" && kind != "benchmarks/redis" && kind != "benchmarks/util") {
				if err := recordCPUResources(visit, kind, active, products); err != nil {
					return err
				}
			}
		}
	}
}

func TestWorkerCounterScopeAdmission(t *testing.T) {
	membership, err := os.ReadFile("/proc/self/cgroup")
	if err != nil {
		t.Fatal(err)
	}
	group := strings.TrimSpace(string(membership))
	if !strings.HasPrefix(group, "0::/") || strings.ContainsAny(group, "\n.") {
		t.Fatalf("unsupported own cgroup membership %q", group)
	}
	parent := filepath.Join("/sys/fs/cgroup", strings.TrimPrefix(group, "0::/"))
	owned := filepath.Join(parent, "gvisor-counter-"+rand.Text())
	if err := os.Mkdir(owned, 0700); err != nil {
		t.Fatalf("create owned counter cgroup: %v", err)
	}
	t.Cleanup(func() {
		if err := os.Remove(owned); err != nil {
			t.Errorf("remove owned counter cgroup: %v", err)
		} else {
			t.Log("owned counter cgroup removed")
		}
	})
	pid := []byte(strconv.Itoa(os.Getpid()))
	if err := os.WriteFile(filepath.Join(owned, "cgroup.procs"), pid, 0600); err != nil {
		t.Fatalf("move own process into owned counter cgroup: %v", err)
	}
	t.Cleanup(func() {
		if err := os.WriteFile(filepath.Join(parent, "cgroup.procs"), pid, 0600); err != nil {
			t.Errorf("restore own process cgroup: %v", err)
		} else {
			t.Log("own process cgroup restored")
		}
	})
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	var original, selected unix.CPUSet
	if err := unix.SchedGetaffinity(0, &original); err != nil {
		t.Fatal(err)
	}
	if !original.IsSet(2) {
		t.Fatal("required existing worker CPU2 is unavailable")
	}
	selected.Set(2)
	if err := unix.SchedSetaffinity(0, &selected); err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := unix.SchedSetaffinity(0, &original); err != nil {
			t.Errorf("restore own thread affinity: %v", err)
		}
	}()
	groupFD, err := unix.Open(owned, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	fds := []int{groupFD}
	defer func() {
		for i := len(fds) - 1; i >= 0; i-- {
			if err := unix.Close(fds[i]); err != nil {
				t.Errorf("close owned counter fd: %v", err)
			}
		}
	}()
	t.Logf("same-guest disabled admission: uid=%d guest_cpus=%d cpu=2", os.Geteuid(), runtime.NumCPU())
	for _, path := range []string{"/proc/sys/kernel/perf_event_paranoid", "/sys/bus/event_source/devices/cpu/type"} {
		data, err := os.ReadFile(path)
		t.Logf("counter support path=%q value=%q read_error=%v", path, strings.TrimSpace(string(data)), err)
	}
	for _, counter := range []struct {
		name   string
		typeID uint32
		config uint64
	}{
		{"software_cpu_clock", unix.PERF_TYPE_SOFTWARE, unix.PERF_COUNT_SW_CPU_CLOCK},
		{"cycles", unix.PERF_TYPE_HARDWARE, unix.PERF_COUNT_HW_CPU_CYCLES},
		{"instructions", unix.PERF_TYPE_HARDWARE, unix.PERF_COUNT_HW_INSTRUCTIONS},
	} {
		attr := unix.PerfEventAttr{
			Type:        counter.typeID,
			Size:        unix.PERF_ATTR_SIZE_VER0,
			Config:      counter.config,
			Read_format: unix.PERF_FORMAT_GROUP | unix.PERF_FORMAT_TOTAL_TIME_ENABLED | unix.PERF_FORMAT_TOTAL_TIME_RUNNING,
			Bits:        unix.PerfBitDisabled | unix.PerfBitExcludeHv,
		}
		for _, scope := range []struct {
			name  string
			pid   int
			flags int
		}{
			{"own_thread", 0, unix.PERF_FLAG_FD_CLOEXEC},
			{"owned_cgroup", groupFD, unix.PERF_FLAG_FD_CLOEXEC | unix.PERF_FLAG_PID_CGROUP},
		} {
			scopedAttr := attr
			fd, err := unix.PerfEventOpen(&scopedAttr, scope.pid, 2, -1, scope.flags)
			errno := 0
			if code, ok := err.(unix.Errno); ok {
				errno = int(code)
			}
			t.Logf("counter=%s scope=%s cpu=2 opened=%t errno=%d error=%v", counter.name, scope.name, err == nil, errno, err)
			if err == nil {
				if err := unix.Close(fd); err != nil {
					t.Errorf("close disabled %s/%s counter: %v", counter.name, scope.name, err)
				}
			}
		}
	}
}
