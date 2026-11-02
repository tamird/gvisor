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

package tools

import (
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
)

// TestHeyImage exercises the packaged client with the command and parsers used
// by network benchmarks. HTTPS tests protocol support, not certificate trust:
// upstream hey intentionally disables certificate verification.
func TestHeyImage(t *testing.T) {
	for _, protocol := range []string{"http", "https"} {
		t.Run(protocol, func(t *testing.T) {
			var requests atomic.Int32
			server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				if got, want := r.Method, http.MethodGet; got != want {
					t.Errorf("method = %q, want %q", got, want)
				}
				if got, want := r.URL.Path, "/probe"; got != want {
					t.Errorf("path = %q, want %q", got, want)
				}
				if _, err := io.WriteString(w, "hey image response\n"); err != nil {
					t.Errorf("write response: %v", err)
				}
			}))
			if protocol == "https" {
				server.StartTLS()
			} else {
				server.Start()
			}
			defer server.Close()
			_, portString, err := net.SplitHostPort(server.Listener.Addr().String())
			if err != nil {
				t.Fatal(err)
			}
			port, err := strconv.Atoi(portString)
			if err != nil {
				t.Fatal(err)
			}
			hey := Hey{Requests: 12, Concurrency: 3, Doc: "probe"}
			command := hey.MakeCmd("localhost", port)
			if protocol == "https" {
				command[len(command)-1] = strings.Replace(command[len(command)-1], "http://", "https://", 1)
			}
			args := []string{"run", "--rm", "--pull=never", "--runtime=runc", "--network=host", "gvisor.dev/images/benchmarks/hey:latest"}
			raw, err := exec.CommandContext(t.Context(), "docker", append(args, command...)...).CombinedOutput()
			output := string(raw)
			if err != nil {
				t.Fatalf("packaged hey: %v\n%s", err, output)
			}
			t.Logf("packaged hey output:\n%s", output)
			if got, want := requests.Load(), int32(hey.Requests); got != want {
				t.Errorf("request count = %d, want %d", got, want)
			}
			if strings.Contains(output, "Error distribution:") {
				t.Errorf("hey reported request errors:\n%s", output)
			}
			qps, err := hey.parseRequestsPerSecond(output)
			if err != nil || qps <= 0 {
				t.Errorf("parsed request rate = %g, error = %v; want positive rate", qps, err)
			}
			latency, err := hey.parseAverageLatency(output)
			if err != nil || latency < 0 {
				t.Errorf("parsed latency = %g, error = %v; want nonnegative latency", latency, err)
			}
		})
	}
}
