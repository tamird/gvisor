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
	"gvisor.dev/gvisor/pkg/test/testutil"
)

var (
	pairedBase        = flag.String("paired-base-runsc", "", "declared baseline release entrypoint")
	pairedCandidate   = flag.String("paired-candidate-runsc", "", "declared candidate release entrypoint")
	pairedBaseID      = flag.String("paired-base-source", "", "baseline runtime source commit")
	pairedCandidateID = flag.String("paired-candidate-source", "", "candidate runtime source commit")
	pairedBinary      = flag.String("paired-benchmark", "", "shared declared benchmark executable")
	pairedImages      = flag.String("paired-image-archives", "", "space-separated declared image archives")
)

func TestPairedBenchmark(t *testing.T) {
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
	for visit, index := range []int{0, 1, 1, 0} {
		arm := arms[index]
		t.Run(fmt.Sprintf("visit%d/%s", visit, arm.name), func(t *testing.T) {
			if err := json.NewEncoder(os.Stdout).Encode(struct {
				Visit    int    `json:"paired_visit"`
				Arm      string `json:"arm"`
				Source   string `json:"runtime_source"`
				Process  int    `json:"pair_process"`
				RunscSHA string `json:"runsc_entrypoint_sha256"`
				NodeSHA  string `json:"benchmark_sha256"`
			}{visit, arm.name, arm.source, os.Getpid(), arm.digest, binarySHA}); err != nil {
				t.Fatal(err)
			}
			cmd := exec.CommandContext(ctx, binary,
				"-test.v=false", "--runtime="+runtimes[index], "--config_path="+config,
				"-test.run=^$", "-test.bench=^BenchmarkNode$", "-test.benchtime=1000x")
			observeCtx, stopObserver := context.WithCancel(ctx)
			observed := make(chan error, 1)
			messages, failures := cli.Events(observeCtx, events.ListOptions{Filters: filters.NewArgs(filters.Arg("type", "container"), filters.Arg("event", "start"))})
			go func() { observed <- attestBenchmarkContainers(observeCtx, cli, messages, failures, visit) }()
			output, err := cmd.CombinedOutput()
			stopObserver()
			if observeErr := <-observed; observeErr != nil {
				t.Error(observeErr)
			}
			if _, writeErr := os.Stdout.Write(output); writeErr != nil {
				t.Fatal(writeErr)
			}
			if err != nil {
				t.Fatal(err)
			}
			for _, concurrency := range []int{1, 5, 10, 25} {
				if strings.Count(string(output), fmt.Sprintf("BenchmarkNode/concurrency.%d-", concurrency)) != 1 {
					t.Fatalf("missing or duplicate completed concurrency %d result", concurrency)
				}
			}
		})
	}
}

// attestBenchmarkContainers observes the actual workload containers without
// adding inspection calls to the benchmark's timed request sequence.
func attestBenchmarkContainers(ctx context.Context, cli *client.Client, messages <-chan events.Message, failures <-chan error, visit int) error {
	seen := make(map[string]int)
	for {
		select {
		case <-ctx.Done():
			for _, image := range []string{"benchmarks/node", "benchmarks/hey"} {
				if seen[image] < 4 {
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
			for _, candidate := range []string{"benchmarks/node", "benchmarks/hey"} {
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
			// count toward the four measured leaves required for this visit.
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
			if kind == "benchmarks/hey" {
				want = ClientCPUSet()
			}
			if want == "" || state.HostConfig.CpusetCpus != want || effective != want {
				return fmt.Errorf("workload %s requested CPUs %q, effective %q, want %q", kind, state.HostConfig.CpusetCpus, effective, want)
			}
			if err := json.NewEncoder(os.Stdout).Encode(struct {
				Visit     int    `json:"affinity_visit"`
				Image     string `json:"image"`
				Container string `json:"container"`
				Runtime   string `json:"runtime"`
				Requested string `json:"requested_cpus"`
				Effective string `json:"effective_cpus"`
			}{visit, image, state.Name, state.HostConfig.Runtime, want, effective}); err != nil {
				return err
			}
			seen[kind]++
		}
	}
}
