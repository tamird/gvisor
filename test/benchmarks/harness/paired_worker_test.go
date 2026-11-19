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
			cmd := exec.CommandContext(ctx, binary,
				"-test.v="+strconv.FormatBool(*pairedResponseProbe), "--runtime="+runtimes[index], "--config_path="+config,
				"-test.run=^$", "-test.bench="+*pairedFilter, "-test.benchtime="+*pairedBenchtime,
				"--node-warmup-requests="+strconv.Itoa(*pairedNodeWarmup),
				"--node-response-probe="+strconv.FormatBool(*pairedResponseProbe),
				"--client-cpuset="+ClientCPUSet(), "--server-cpuset="+ServerCPUSet(),
				"--node-redis-cpuset="+*pairedRedisCPUs, "--hey-phase-diagnostics")
			var observed chan error
			var stopObserver context.CancelFunc
			if *pairedObserve {
				observeCtx, cancel := context.WithCancel(ctx)
				stopObserver = cancel
				observed = make(chan error, 1)
				messages, failures := cli.Events(observeCtx, events.ListOptions{Filters: filters.NewArgs(filters.Arg("type", "container"), filters.Arg("event", "start"))})
				go func() {
					observed <- attestBenchmarkContainers(observeCtx, cli, messages, failures, visit, releaseProducts[index], actualHashes)
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
func attestBenchmarkContainers(ctx context.Context, cli *client.Client, messages <-chan events.Message, failures <-chan error, visit int, products, actualHashes map[string]string) error {
	seen := make(map[string]int)
	active := make(map[int]string)
	for {
		select {
		case <-ctx.Done():
			required := []string{"benchmarks/node", "benchmarks/hey", "benchmarks/redis"}
			if *pairedResponseProbe {
				required[1] = "benchmarks/util"
			}
			for _, image := range required {
				if seen[image] < *pairedLeaves {
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
			for _, candidate := range []string{"benchmarks/node", "benchmarks/hey", "benchmarks/redis", "benchmarks/util"} {
				if candidate == "benchmarks/util" && !*pairedResponseProbe {
					continue
				}
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
			if kind == "benchmarks/util" && state.HostConfig.CpusetCpus == "" {
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
			if (kind != "benchmarks/redis" || want != "") && (want == "" || state.HostConfig.CpusetCpus != want || effective != want) {
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
			if kind == "benchmarks/node" && products[exe] != digest {
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
			if kind == "benchmarks/hey" {
				if err := recordCPUResources(visit, kind, active, products); err != nil {
					return err
				}
			}
		}
	}
}
