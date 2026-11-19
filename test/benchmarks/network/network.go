// Copyright 2020 The gVisor Authors.
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

// Package network holds benchmarks around raw network performance.
package network

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/test/benchmarks/harness"
	"gvisor.dev/gvisor/test/benchmarks/tools"
	"gvisor.dev/gvisor/test/metricsviz"
)

var heyPhaseDiagnostics = flag.Bool("hey-phase-diagnostics", false, "retain Hey phase reports and CPU endpoints in benchmark logs")

// runStaticServer runs static serving workloads (httpd, nginx).
func runStaticServer(b *testing.B, serverOpts dockerutil.RunOpts, serverCmd []string, port int, hey *tools.Hey) {
	ctx := context.Background()

	// Get two machines: a client and server.
	clientMachine, err := harness.GetMachine()
	if err != nil {
		b.Fatalf("failed to get machine: %v", err)
	}
	defer clientMachine.CleanUp()

	serverMachine, err := harness.GetMachine()
	if err != nil {
		b.Fatalf("failed to get machine: %v", err)
	}
	defer serverMachine.CleanUp()

	// Make the containers.
	client := clientMachine.GetNativeContainer(ctx, b)
	defer client.CleanUp(ctx)
	server := serverMachine.GetContainer(ctx, b)
	defer server.CleanUp(ctx)
	defer metricsviz.FromContainerLogs(ctx, b, server)

	// Set CPU affinity if not already explicitly configured.
	if serverOpts.CpusetCpus == "" {
		serverOpts.CpusetCpus = harness.ServerCPUSet()
	}

	// Start the server.
	if err := server.Spawn(ctx, serverOpts, serverCmd...); err != nil {
		b.Fatalf("failed to start server: %v", err)
	}

	// Make sure the server is serving.
	if err := harness.WaitUntilContainerServing(ctx, clientMachine, server, port); err != nil {
		b.Fatalf("server readiness failed: %v", err)
	}

	runHey(b, ctx, client, dockerutil.RunOpts{
		Image:      "benchmarks/hey",
		Links:      []string{server.MakeLink("server")},
		CpusetCpus: harness.ClientCPUSet(),
	}, hey, "measured", []*dockerutil.Container{server}, hey.MakeCmd("server", port)...)
}

// runHey stops timing before reading and reporting the client's output.
func runHey(b *testing.B, ctx context.Context, client *dockerutil.Container, opts dockerutil.RunOpts, hey *tools.Hey, phase string, servers []*dockerutil.Container, args ...string) {
	b.Helper()
	var before, after heyCPUCounters
	var beforeBegin, beforeDone, start, end, afterDone int64
	var captureErr error
	if *heyPhaseDiagnostics {
		beforeBegin = heyMonotonicNS(b)
		before, captureErr = readHeyCPUCounters(ctx, servers)
		if captureErr != nil {
			b.Fatalf("capture %s start counters: %v", phase, captureErr)
		}
		beforeDone = heyMonotonicNS(b)
	}
	b.ResetTimer()
	if *heyPhaseDiagnostics {
		start = heyMonotonicNS(b)
	}
	if err := client.Spawn(ctx, opts, args...); err != nil {
		b.Fatalf("failed to start hey: %v", err)
	}
	waitErr := client.Wait(ctx)
	b.StopTimer()
	if *heyPhaseDiagnostics {
		end = heyMonotonicNS(b)
		after, captureErr = readHeyCPUCounters(ctx, servers)
		afterDone = heyMonotonicNS(b)
		if captureErr != nil {
			b.Logf("capture %s end counters: %v", phase, captureErr)
		}
	}
	stdout, stderr, err := client.OutputStreams(ctx)
	if err != nil {
		b.Fatalf("failed to read hey output: %v", err)
	}
	if stderr.Len() != 0 {
		b.Logf("hey stderr: %s", stderr)
	}
	if waitErr != nil {
		b.Fatalf("hey failed: %v\nstdout: %s", waitErr, stdout)
	}
	if captureErr != nil {
		b.Fatalf("capture %s end counters: %v; Hey report: %s", phase, captureErr, stdout.Bytes())
	}
	hey.Report(b, stdout.Bytes())
	if *heyPhaseDiagnostics {
		payload, err := json.Marshal(struct {
			Phase       string          `json:"hey_phase"`
			Benchmark   string          `json:"benchmark"`
			Iterations  int             `json:"benchmark_iterations"`
			Requests    int             `json:"requests"`
			Client      string          `json:"client_id"`
			BeforeBegin int64           `json:"before_counters_begin_monotonic_ns"`
			BeforeDone  int64           `json:"before_counters_end_monotonic_ns"`
			Start       int64           `json:"start_monotonic_ns"`
			End         int64           `json:"end_monotonic_ns"`
			AfterDone   int64           `json:"after_counters_end_monotonic_ns"`
			Before      heyCPUCounters  `json:"before"`
			After       heyCPUCounters  `json:"after"`
			Report      json.RawMessage `json:"report"`
		}{phase, b.Name(), b.N, hey.Requests, client.ID(), beforeBegin, beforeDone, start, end, afterDone, before, after, stdout.Bytes()})
		if err != nil {
			b.Fatal(err)
		}
		b.Logf("%s", payload)
	}
}

type heyCPUCounters struct {
	CPUs    string            `json:"guest_cpu"`
	Cgroups map[string]string `json:"server_cgroups"`
}

// readHeyCPUCounters runs outside the timer while the servers are still live.
// Native client CPU time remains covered by the guest per-CPU counters; its
// cgroup may disappear as soon as Wait returns.
func readHeyCPUCounters(ctx context.Context, servers []*dockerutil.Container) (heyCPUCounters, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	stat, err := os.ReadFile("/proc/stat")
	if err != nil {
		return heyCPUCounters{}, err
	}
	var cpuLines []string
	for _, line := range strings.Split(string(stat), "\n") {
		if strings.HasPrefix(line, "cpu") {
			cpuLines = append(cpuLines, line)
		}
	}
	result := heyCPUCounters{strings.Join(cpuLines, "\n"), make(map[string]string)}
	for _, server := range servers {
		state, err := server.Status(ctx)
		if err != nil {
			return heyCPUCounters{}, err
		}
		if state.Pid <= 0 || !state.Running {
			return heyCPUCounters{}, fmt.Errorf("server %s is no longer live at the phase boundary", server.ID())
		}
		membership, err := os.ReadFile(fmt.Sprintf("/proc/%d/cgroup", state.Pid))
		if err != nil {
			return heyCPUCounters{}, err
		}
		group := strings.TrimSpace(string(membership))
		if !strings.HasPrefix(group, "0::/") || strings.Contains(group, "\n") {
			return heyCPUCounters{}, fmt.Errorf("unsupported phase cgroup membership %q", group)
		}
		path := filepath.Join("/sys/fs/cgroup", strings.TrimPrefix(group, "0::/"), "cpu.stat")
		counters, err := os.ReadFile(path)
		if err != nil {
			return heyCPUCounters{}, err
		}
		result.Cgroups[path] = string(counters)
	}
	return result, nil
}

func heyMonotonicNS(b *testing.B) int64 {
	b.Helper()
	var ts unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_MONOTONIC, &ts); err != nil {
		b.Fatal(err)
	}
	return ts.Sec*1e9 + ts.Nsec
}
