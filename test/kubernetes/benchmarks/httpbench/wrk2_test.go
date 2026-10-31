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

package httpbench

import (
	"context"
	"flag"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/testutil"
	"gvisor.dev/gvisor/test/kubernetes/testcluster"
)

var wrk2Binary = flag.String("wrk2", "", "Declared wrk2 executable in the test runfiles")

func TestWrk2Consumer(t *testing.T) {
	binary, err := testutil.FindFile(*wrk2Binary)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintln(w, "wrk2 benchmark response")
	}))
	defer server.Close()
	host, portText, err := net.SplitHostPort(server.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	port, err := strconv.Atoi(portText)
	if err != nil {
		t.Fatal(err)
	}
	benchmark := HTTPBenchmark{
		Namespace: &testcluster.Namespace{Namespace: "wrk2-test"},
		Port:      port,
		Path:      "/",
		Timeout:   5 * time.Second,
	}
	// Keep a measured interval after wrk2's ten-second calibration.
	round := Round{NumThreads: 2, TargetQPS: 64, Duration: 12 * time.Second}
	pod := benchmark.newWrk2Client("wrk2-test", host, round)
	args := pod.Spec.Containers[0].Command
	if got, want := args[0], "wrk2"; got != want {
		t.Fatalf("command = %q, want %q", got, want)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, binary, args[1:]...).CombinedOutput()
	if err != nil {
		t.Fatalf("wrk2 %q: %v\n%s", args[1:], err, out)
	}
	t.Logf("wrk2 %q\n%s", args[1:], out)
	requests, metrics, err := getMeasurements(string(out), nil, []int{50, 99})
	if err != nil {
		t.Fatal(err)
	}
	if requests <= 0 {
		t.Errorf("requests = %d, want positive", requests)
	}
	if got, want := len(metrics), 4; got != want {
		t.Fatalf("metric count = %d, want %d: %v", got, want, metrics)
	}
	for _, metric := range metrics {
		text := metric.ToBenchstat()
		fields := strings.Fields(text)
		if got, want := len(fields), 2; got != want {
			t.Fatalf("metric = %q, want %d fields, got %d", text, want, got)
		}
		value, err := strconv.ParseFloat(fields[0], 64)
		if err != nil || value <= 0 {
			t.Errorf("metric = %q, want positive value: %v", text, err)
		}
	}
}
