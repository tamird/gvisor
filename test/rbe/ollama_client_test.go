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

package rbe

import (
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
)

// TestOllamaClientImage checks the actual packaged client without an Ollama
// server, model download or GPU. The native container shares this test VM's
// network namespace so the request reaches its local HTTP server.
func TestOllamaClientImage(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read request: %v", err)
		}
		if got, want := r.Method, http.MethodPost; got != want {
			t.Errorf("request method = %q, want %q", got, want)
		}
		if got, want := r.URL.Path, "/probe"; got != want {
			t.Errorf("request path = %q, want %q", got, want)
		}
		if got, want := r.Header.Get("X-Client"), "bazel"; got != want {
			t.Errorf("request header = %q, want %q", got, want)
		}
		if got, want := string(body), "declared client"; got != want {
			t.Errorf("request body = %q, want %q", got, want)
		}
		w.Header().Set("X-Image", "declared")
		if _, err := io.WriteString(w, "data:first\ndata:last"); err != nil {
			t.Errorf("write response: %v", err)
		}
	}))
	defer server.Close()
	ctx := t.Context()
	container := dockerutil.MakeNativeContainer(ctx, t)
	defer container.CleanUp(ctx)
	output, err := container.Run(ctx, dockerutil.RunOpts{
		Image:       "gpu/ollama/client",
		NetworkMode: "host",
	}, "httpclient", "--url="+server.URL+"/probe", "--method=POST", "--header=X-Client: bazel", "--post_base64="+base64.StdEncoding.EncodeToString([]byte("declared client")), "--strip_prefix=data:")
	if err != nil {
		t.Fatalf("packaged client: %v\n%s", err, output)
	}
	if got, want := requests.Load(), int32(1); got != want {
		t.Errorf("HTTP requests = %d, want %d", got, want)
	}
	for _, marker := range []string{"BODY: \"first\\n\"", "BODY: \"last\"", "REQHEADER: X-Client: bazel", "RESPHEADER: X-Image: declared"} {
		if !strings.Contains(output, marker) {
			t.Errorf("missing %q in client output:\n%s", marker, output)
		}
	}
	var stats map[string]time.Time
	for _, line := range strings.Split(output, "\n") {
		if encoded, ok := strings.CutPrefix(line, "STATS: "); ok {
			if err := json.Unmarshal([]byte(encoded), &stats); err != nil {
				t.Fatalf("client statistics: %v", err)
			}
		}
	}
	for _, name := range []string{"program_started", "request_sent", "response_received", "first_byte_read", "last_byte_read"} {
		if stats[name].IsZero() {
			t.Errorf("missing client timestamp %q in output:\n%s", name, output)
		}
	}

	t.Run("system_certificates", func(t *testing.T) {
		container := dockerutil.MakeNativeContainer(ctx, t)
		defer container.CleanUp(ctx)
		// Use the shipped trust bundle without mounting or injecting a test CA.
		output, err := container.Run(ctx, dockerutil.RunOpts{
			Image:       "gpu/ollama/client",
			NetworkMode: "host",
		}, "httpclient", "--url=https://gvisor.dev/", "--timeout=30s")
		if err != nil {
			t.Fatalf("HTTPS with packaged CA trust: %v\n%s", err, output)
		}
		for _, marker := range []string{"BODY:", "STATS:"} {
			if !strings.Contains(output, marker) {
				t.Errorf("missing %q in HTTPS output:\n%s", marker, output)
			}
		}
	})
}
