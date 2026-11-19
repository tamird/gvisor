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
	"strings"
	"testing"
	"time"

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
		t.Run("affinity/"+arm.name, func(t *testing.T) {
			oldRuntime, oldEntrypoint := *workerRuntime, *runscEntrypoint
			defer func() { *workerRuntime, *runscEntrypoint = oldRuntime, oldEntrypoint }()
			*workerRuntime, *runscEntrypoint = "runsc", arm.runsc
			testDockerWorker(t, true)
		})
	}
	if t.Failed() {
		return
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
				"-test.v=false", "--owned-docker", "--worker-runsc="+arm.runsc,
				"--worker-image-archives="+strings.Join(strings.Fields(*pairedImages), ","),
				"-test.run=^$", "-test.bench=^BenchmarkNode$", "-test.benchtime=25x")
			output, err := cmd.CombinedOutput()
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
