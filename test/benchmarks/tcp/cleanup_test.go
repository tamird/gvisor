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

package tcp_test

import (
	"bufio"
	"bytes"
	"context"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/testutil"
)

var iperfBinary = flag.String("iperf-binary", "", "declared iperf3 executable")

func TestMain(m *testing.M) {
	if program, ok := os.LookupEnv("TCP_BENCHMARK_TEST_PROXY"); ok {
		// Real services handle traffic. Their parents resist cleanup's TERM.
		signal.Ignore(syscall.SIGTERM)
		role := "proxy"
		if filepath.Base(os.Args[0]) == "iperf3" {
			program, _ = os.LookupEnv("TCP_BENCHMARK_TEST_IPERF")
			role = "client"
			if slices.Contains(os.Args[1:], "-s") {
				role = "server"
			}
		}
		if stall, _ := os.LookupEnv("TCP_BENCHMARK_TEST_STALL"); stall == role {
			time.Sleep(time.Hour)
		}
		cmd := exec.Command(program, os.Args[1:]...)
		cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
		if err := cmd.Run(); err != nil {
			os.Exit(1)
		}
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func TestBenchmarkLifecycle(t *testing.T) {
	for _, tc := range []struct {
		name, duration, wantError string
		timeout                   time.Duration
	}{
		{"cleanup", "08", "", 60 * time.Second},
		{"server", "1", "iperf server failed to listen: context deadline exceeded", 40 * time.Second},
		{"proxy", "1", "client proxy failed to listen: context deadline exceeded", 95 * time.Second},
		{"client", "030", "iperf client did not finish within 1m30s: context deadline exceeded", 135 * time.Second},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			self, err := os.Executable()
			if err != nil {
				t.Fatal(err)
			}
			find := func(path string) string {
				t.Helper()
				file, err := testutil.FindFile("test/benchmarks/tcp/" + path)
				if err != nil {
					t.Fatal(err)
				}
				return file
			}
			helpers, err := os.MkdirTemp(testutil.TmpDir(), "tcp-cleanup-")
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = os.RemoveAll(helpers) })
			for name, path := range map[string]string{
				"tcp_proxy": self,
				"iperf3":    self,
				"nsjoin":    find("nsjoin"),
			} {
				if err := os.Symlink(path, filepath.Join(helpers, name)); err != nil {
					t.Fatal(err)
				}
			}
			iperf, err := testutil.FindFile(*iperfBinary)
			if err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), tc.timeout)
			defer cancel()
			cmd := exec.CommandContext(ctx, find("tcp_benchmark"),
				"--helpers", helpers, "--iperf-binary", filepath.Join(helpers, "iperf3"),
				"--ideal", "--duration", tc.duration, "--no-user-ns")
			cmd.Env = append(os.Environ(),
				"TCP_BENCHMARK_TEST_PROXY="+find("tcp_proxy"),
				"TCP_BENCHMARK_TEST_IPERF="+iperf,
				"TCP_BENCHMARK_TEST_STALL="+tc.name)
			// Kill the coordinator and its private PID namespace on cancellation.
			cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
			cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
			cmd.WaitDelay = 2 * time.Second
			var stderr bytes.Buffer
			cmd.Stderr = &stderr
			stdout, err := cmd.StdoutPipe()
			if err != nil {
				t.Fatal(err)
			}
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			result := bufio.NewScanner(stdout)
			if tc.wantError != "" {
				if result.Scan() {
					cancel()
					_ = cmd.Wait()
					t.Fatalf("stalled service produced a result: %q", result.Text())
				}
				if err := cmd.Wait(); err == nil || ctx.Err() != nil || result.Err() != nil ||
					!strings.Contains(stderr.String(), tc.wantError) {
					t.Fatalf("stalled service: %v (context %v, stdout %v), want %q:\n%s",
						err, ctx.Err(), result.Err(), tc.wantError, &stderr)
				}
				return
			}
			result.Scan()
			var name string
			var iterations int
			var throughput, cpu float64
			n, scanErr := fmt.Sscanf(result.Text(), "%s %d %f Mb/s %f cpu-time", &name, &iterations, &throughput, &cpu)
			if scanErr != nil || n != 4 || !strings.HasPrefix(name, "BenchmarkTCP/") ||
				iterations != 1 || throughput <= 0 {
				cancel()
				_ = cmd.Wait()
				t.Fatalf("missing benchmark result: %q (scan error %v):\n%s", result.Text(), result.Err(), &stderr)
			}
			// Start the cleanup budget only after a real transfer has produced output.
			timer := time.AfterFunc(20*time.Second, cancel)
			defer timer.Stop()
			if result.Scan() {
				t.Errorf("unexpected additional output: %q", result.Text())
			}
			if err := cmd.Wait(); err != nil || ctx.Err() != nil || result.Err() != nil {
				t.Fatalf("cleanup failed: %v (context %v, stdout %v):\n%s", err, ctx.Err(), result.Err(), &stderr)
			}
		})
	}
}
