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
	"maps"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/image"
	"github.com/docker/docker/client"
	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

var (
	pairedBase		= flag.String("paired-base-runsc", "", "declared baseline release entrypoint")
	pairedCandidate		= flag.String("paired-candidate-runsc", "", "declared control release entrypoint")
	pairedBaseID		= flag.String("paired-base-source", "", "baseline production runtime source commit")
	pairedCandidateID	= flag.String("paired-candidate-source", "", "control production runtime source commit")
	pairedBinary		= flag.String("paired-benchmark", "", "shared declared benchmark executable")
	pairedImages		= flag.String("paired-image-archives", "", "space-separated declared image archives")
	pairedFilter		= flag.String("paired-bench", "^Benchmark", "ordinary Go benchmark selection expression")
	pairedBenchtime		= flag.String("paired-benchtime", "", "explicit ordinary Go benchmark time or operation count")
	pairedBlocks		= flag.Int("paired-blocks", 1, "number of balanced BASE/CONTROL/CONTROL/BASE blocks")
	pairedLeaves		= flag.Int("paired-leaves", 0, "number of selected benchmark leaves per visit")
	pairedWarmups		= flag.Int("paired-warmup-pairs", 0, "unmeasured warmup visits for each runtime before balanced blocks")
	pairedNative		= flag.Bool("paired-native-control", false, "use the existing native runc runtime for one diagnostic arm")
)

func TestPairedBenchmark(t *testing.T) {
	if *pairedBlocks < 1 || *pairedLeaves < 1 || *pairedWarmups < 0 {
		t.Fatal("paired blocks and leaves must be positive")
	}
	if *pairedBaseID != *pairedCandidateID {
		t.Fatal("same-source control requires identical production source commits")
	}
	if *pairedBenchtime == "" || *pairedImages == "" {
		t.Fatal("paired control requires explicit benchtime and declared image archives")
	}
	var available unix.CPUSet
	if err := unix.SchedGetaffinity(0, &available); err != nil {
		t.Fatal(err)
	}
	for cpu := 0; cpu <= 3; cpu++ {
		if !available.IsSet(cpu) {
			t.Fatalf("required worker CPU %d is unavailable; affinity: %v", cpu, available)
		}
	}
	if available.Count() != 4 {
		// A worker can expose additional guest CPUs beyond its reservation.
		// Re-exec before creating Docker so all new threads and children
		// inherit the declared partition.
		var partition unix.CPUSet
		for cpu := 0; cpu <= 3; cpu++ {
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
		{"CONTROL", *pairedCandidate, *pairedCandidateID, ""},
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
		arm.runsc, err = filepath.EvalSymlinks(path)
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
	if !maps.Equal(releaseProducts[0], releaseProducts[1]) {
		t.Fatal("same-source control requires the same complete immutable release products")
	}
	if err := json.NewEncoder(os.Stdout).Encode(struct {
		Products []map[string]string `json:"opaque_release_products"`
	}{releaseProducts}); err != nil {
		t.Fatal(err)
	}
	host, runtimes, config := startDockerDaemons(t, ctx, []string{arms[0].runsc, arms[1].runsc}, true)
	releaseEntrypoints := []string{arms[0].runsc, arms[1].runsc}
	if *pairedNative {
		nativePath, err := exec.LookPath("runc")
		if err != nil {
			t.Fatal(err)
		}
		nativePath, err = filepath.EvalSymlinks(nativePath)
		if err != nil {
			t.Fatal(err)
		}
		nativeDigest, err := executableDigest(nativePath)
		if err != nil {
			t.Fatal(err)
		}
		version, err := exec.CommandContext(ctx, nativePath, "--version").CombinedOutput()
		if err != nil {
			t.Fatalf("native runtime identity failed: %v: %s", err, version)
		}
		t.Logf("native runtime %s sha256=%s version=%q", nativePath, nativeDigest, strings.TrimSpace(string(version)))
		arms[0].name = "SENTRY"
		arms[1].name = "NATIVE"
		arms[1].source = "native-runc:" + nativeDigest
		arms[1].digest = ""
		runtimes[1] = "runc"
		releaseEntrypoints[1] = ""
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
	images, err := cli.ImageList(ctx, image.ListOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if err := json.NewEncoder(os.Stdout).Encode(struct {
		Server   string          `json:"shared_docker_version"`
		Images   []image.Summary `json:"images"`
		Runtimes []string        `json:"registered_runtimes"`
		Releases []string        `json:"release_entrypoints"`
	}{version.Version, images, runtimes, releaseEntrypoints}); err != nil {
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
				BenchSHA string `json:"benchmark_sha256"`
			}{visit, workerID, block, warmup, arm.name, arm.source, os.Getpid(), arm.digest, binarySHA}); err != nil {
				t.Fatal(err)
			}
			args := []string{
				"-test.v=false", "--runtime=" + runtimes[index], "--config_path=" + config,
				"-test.run=^$", "-test.bench=" + *pairedFilter, "-test.benchtime=" + *pairedBenchtime,
			}
			args = append(args, "--owned-docker", "--client-cpuset="+ClientCPUSet(), "--server-cpuset="+ServerCPUSet())
			cmd := exec.CommandContext(ctx, binary, args...)
			output, err := cmd.CombinedOutput()
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
			cleanupCtx, cleanupCancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
			containers, listErr := cli.ContainerList(cleanupCtx, container.ListOptions{All: true})
			cleanupCancel()
			if listErr != nil {
				t.Error(listErr)
			} else if len(containers) != 0 {
				t.Errorf("owned containers remain after visit %d: %v", visit, containers)
			}
			for i, release := range arms {
				products, hashErr := releaseDigests(filepath.Dir(release.runsc))
				if hashErr != nil {
					t.Error(hashErr)
				} else if !maps.Equal(products, releaseProducts[i]) {
					t.Errorf("complete release products changed after visit %d", visit)
				}
			}
			if _, writeErr := os.Stdout.Write(output); writeErr != nil {
				t.Fatal(writeErr)
			}
			if err != nil {
				t.Fatal(err)
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
