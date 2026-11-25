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

package main

import (
	"context"
	"errors"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/testutil"
)

func TestComparisonFlags(t *testing.T) {
	benchstat, err := testutil.FindFile(os.Getenv("PAIRED_BENCHSTAT"))
	if err != nil {
		t.Fatal(err)
	}
	metrics := map[string]int{"One-4": 3}
	for _, changed := range []bool{false, true} {
		row := "BenchmarkOne-4 1 10 ns/op 20 bandwidth.bytes_per_second 30 io_ops.ops_per_second\n"
		dir := t.TempDir()
		left, right := filepath.Join(dir, "A"), filepath.Join(dir, "B")
		if err := os.WriteFile(left, []byte(strings.Repeat(row, 24)), 0644); err != nil {
			t.Fatal(err)
		}
		if changed {
			row = strings.ReplaceAll(row, "10 ns/op", "20 ns/op")
		}
		if err := os.WriteFile(right, []byte(strings.Repeat(row, 24)), 0644); err != nil {
			t.Fatal(err)
		}
		data, err := exec.CommandContext(t.Context(), benchstat, "-format=csv", "-row=.fullname", "-col=.file", "-alpha=0.00001388888888888889", "A="+left, "B="+right).Output()
		if err != nil {
			t.Fatal(err)
		}
		if flagged, err := comparisonFlags(data, metrics); err != nil || flagged != changed {
			t.Fatalf("changed=%t: flagged=%t, err=%v, output=%s", changed, flagged, err, data)
		}
		if _, err := comparisonFlags(data, map[string]int{"One-4": 4}); err == nil {
			t.Fatal("accepted an omitted metric")
		}
	}
	for _, data := range []string{"", "\"unterminated"} {
		if _, err := comparisonFlags([]byte(data), metrics); err == nil {
			t.Fatalf("accepted invalid report %q", data)
		}
	}
}

func TestVisitTimeout(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	r := report{
		Commands: map[string]command{
			"A": {Argv: []string{"/bin/sleep", "60"}},
		},
		PinCPU:       -1,
		VisitTimeout: 100 * time.Millisecond,
	}
	err := r.collect(ctx, []string{"One"}, t.TempDir())
	if !errors.Is(err, context.DeadlineExceeded) || ctx.Err() != nil {
		t.Fatalf("visit deadline: got %v, parent context: %v", err, ctx.Err())
	}
	if len(r.Visits) != 1 || r.Visits[0].PID <= 0 || r.Visits[0].ExitCode == nil || *r.Visits[0].ExitCode == 0 {
		t.Fatalf("timed-out child was not reaped with a failure: %+v", r.Visits)
	}
}

func TestAffinity(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("CPU affinity is supported only on Linux")
	}
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	original, err := getAffinity(0)
	if err != nil || len(original) == 0 {
		t.Fatalf("read affinity: %v, %v", original, err)
	}
	for _, mode := range []string{"bound", "unbound", "start failure", "invalid CPU"} {
		cmd := exec.Command("/bin/sleep", "60")
		cpu := original[0]
		switch mode {
		case "unbound":
			cpu = -1
		case "start failure":
			cmd = exec.Command(filepath.Join(t.TempDir(), "missing"))
		case "invalid CPU":
			cpu = original[len(original)-1] + 1
		}
		a, err := startChild(cmd, cpu)
		if cmd.Process != nil {
			t.Cleanup(func() {
				if err := cmd.Process.Kill(); err != nil {
					t.Error(err)
				}
				if err := cmd.Wait(); err == nil {
					t.Error("killed child exited successfully")
				} else if _, ok := errors.AsType[*exec.ExitError](err); !ok {
					t.Errorf("wait for killed child: %v", err)
				}
			})
		}
		wantError := mode == "start failure" || mode == "invalid CPU"
		if (err != nil) != wantError {
			t.Fatalf("%s: affinity=%+v, err=%v", mode, a, err)
		}
		if wantError && cmd.Process != nil {
			t.Fatalf("%s started a child despite returning an error", mode)
		}
		if mode == "bound" {
			observed, err := getAffinity(cmd.Process.Pid)
			if err != nil || !slices.Equal(observed, []int{cpu}) {
				t.Fatalf("bound child CPUs=%v, want [%d], err=%v", observed, cpu, err)
			}
		}
		if mode == "unbound" {
			observed, err := getAffinity(cmd.Process.Pid)
			if a != nil || err != nil || !slices.Equal(observed, original) {
				t.Fatalf("unbound child affinity=%+v, observed=%v, err=%v", a, observed, err)
			}
		}
		after, err := getAffinity(0)
		if err != nil || !slices.Equal(after, original) {
			t.Fatalf("%s changed parent affinity: %v, want %v, err=%v", mode, after, original, err)
		}
	}
}

func TestSchedule(t *testing.T) {
	for _, preflight := range []bool{false, true} {
		orders := schedule(preflight)
		want := 25
		if preflight {
			want = 3
		}
		if len(orders) != want || orders[0] != "AB" {
			t.Fatalf("schedule(%t) = %v", preflight, orders)
		}
		for i, order := range orders[1:] {
			if want := []string{"AB", "BA"}[i%2]; order != want {
				t.Errorf("pair %d = %q, want %q", i, order, want)
			}
		}
	}
}

func TestParse(t *testing.T) {
	header := "goos: " + runtime.GOOS + "\ngoarch: " + runtime.GOARCH + "\ncpu: test\nfuture: retained\n"
	row := "BenchmarkOne 1 10 ns/op 0 B/op 0 allocs/op\n"
	got, err := parse([]byte(header+row+"future: changed\n"+strings.ReplaceAll(row, "One", "Two")), []string{"One", "Two"}, true)
	if err != nil {
		t.Fatal(err)
	}
	if got["One"].GetConfig("future") != "retained" || got["Two"].GetConfig("future") != "changed" || got["One"].Iters != 1 {
		t.Fatalf("configuration or iteration count lost: %+v", got["One"])
	}
	seconds, _ := got["One"].Value("sec/op")
	bytes, haveBytes := got["One"].Value("B/op")
	allocs, haveAllocs := got["One"].Value("allocs/op")
	if len(got["One"].Values) != 3 || math.Abs(seconds-1e-8) > 1e-23 || !haveBytes || bytes != 0 || !haveAllocs || allocs != 0 {
		t.Fatalf("metrics = %v", got["One"].Values)
	}
	if _, err := parse([]byte(strings.ReplaceAll(header, "cpu: test\n", "")+row), []string{"One"}, true); err != nil {
		t.Fatalf("optional CPU metadata: %v", err)
	}
	for _, name := range []string{"One", "One/sub=case", "One/sub=other/nested"} {
		rawName := name + "-8"
		got, err := parse([]byte(header+strings.ReplaceAll(row, "One", rawName)), []string{name}, true)
		if err != nil {
			t.Fatalf("parse %q: %v", rawName, err)
		}
		if got[name].Name.String() != rawName {
			t.Fatalf("benchmark name = %q, want %q", got[name].Name, rawName)
		}
	}
	for _, text := range []string{
		"", // Missing row.
		row + row,
		row + strings.ReplaceAll(row, "One", "One-8"),
		strings.ReplaceAll(row, "One", "Other"),
		strings.ReplaceAll(row, "One 1", "One 2"),
		strings.ReplaceAll(row, "10 ns/op", "oops ns/op"),
		strings.ReplaceAll(row, "10 ns/op", "0 ns/op"),
	} {
		if _, err := parse([]byte(header+text), []string{"One"}, true); err == nil {
			t.Errorf("accepted invalid preflight output %q", text)
		}
	}
}
