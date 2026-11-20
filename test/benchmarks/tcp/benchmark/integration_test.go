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

package main

import (
	"context"
	"flag"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/bazelbuild/rules_go/go/runfiles"
)

var testBenchmark, testProxy, testNSJoin string

func TestMain(m *testing.M) {
	if failure := os.Getenv("TCP_BENCHMARK_INTEGRATION_FAILURE"); failure != "" {
		role, port := "client", ""
		for i, arg := range os.Args[1:] {
			if arg == "-s" {
				role = "server"
			}
			if arg == "-p" && i+2 < len(os.Args) {
				port = os.Args[i+2]
			}
			if arg == "-forward" {
				role = "server-proxy"
				if slices.Contains(os.Args, "-client") {
					role = "client-proxy"
				}
			}
		}
		dir := os.Getenv("TCP_BENCHMARK_TEST_MARKERS")
		ns, err := os.Readlink("/proc/self/ns/pid")
		if err != nil {
			os.Exit(2)
		}
		if err := os.WriteFile(filepath.Join(dir, role+".started"), []byte(ns), 0600); err != nil {
			os.Exit(2)
		}
		if role == "server" && failure != "server" {
			listener, err := net.Listen("tcp", ":"+port)
			if err != nil {
				os.Exit(2)
			}
			defer listener.Close()
		}
		time.Sleep(time.Hour)
		os.Exit(0)
	}
	flag.StringVar(&testBenchmark, "test-benchmark", "", "declared benchmark runfile")
	flag.StringVar(&testProxy, "test-proxy", "", "declared TCP proxy runfile")
	flag.StringVar(&testNSJoin, "test-nsjoin", "", "declared namespace helper runfile")
	os.Exit(m.Run())
}

func TestBenchmarkStalledHelpers(t *testing.T) {
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	benchmark, err := runfiles.Rlocation(testBenchmark)
	if err != nil {
		t.Fatal(err)
	}
	proxy, err := runfiles.Rlocation(testProxy)
	if err != nil {
		t.Fatal(err)
	}
	nsjoin, err := runfiles.Rlocation(testNSJoin)
	if err != nil {
		t.Fatal(err)
	}
	parentNS, err := os.Readlink("/proc/self/ns/pid")
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		role, message string
		timeout       time.Duration
		started       []string
	}{
		{role: "server", message: "iperf server failed to listen: context deadline exceeded", timeout: 40 * time.Second, started: []string{"server"}},
		{role: "proxy", message: "client proxy failed to listen: context deadline exceeded", timeout: 90 * time.Second, started: []string{"server", "server-proxy", "client-proxy"}},
		{role: "client", message: "iperf client did not finish within 1m1s: context deadline exceeded", timeout: 91 * time.Second, started: []string{"server", "client"}},
	} {
		t.Run(tc.role, func(t *testing.T) {
			t.Parallel()
			markers := t.TempDir()
			tcpProxy := proxy
			if tc.role == "proxy" {
				tcpProxy = self
			}
			args := []string{"--client", "--ideal", "--duration=1", "--nsjoin=" + nsjoin, "--tcp-proxy=" + tcpProxy, "--iperf-binary=" + self}
			if os.Geteuid() == 0 {
				args = append(args, "--no-user-ns")
			}
			ctx, cancel := context.WithTimeout(context.Background(), tc.timeout)
			defer cancel()
			cmd := exec.CommandContext(ctx, benchmark, args...)
			cmd.Env = append(os.Environ(), "TCP_BENCHMARK_INTEGRATION_FAILURE="+tc.role, "TCP_BENCHMARK_TEST_MARKERS="+markers)
			output, err := cmd.CombinedOutput()
			if err == nil || ctx.Err() != nil || !strings.Contains(string(output), tc.message) {
				t.Fatalf("benchmark returned %v (context %v), want %q before outer deadline:\n%s", err, ctx.Err(), tc.message, output)
			}
			// CombinedOutput has received EOF from every helper's inherited
			// stderr. The coordinator has exited its private PID namespace.
			for _, role := range tc.started {
				ns, err := os.ReadFile(filepath.Join(markers, role+".started"))
				if err != nil {
					t.Fatalf("%s never started: %v\n%s", role, err, output)
				}
				if string(ns) == parentNS {
					t.Errorf("%s ran in parent PID namespace %s", role, ns)
				}
			}
		})
	}
}
