// Copyright 2026 The gVisor Authors.
// SPDX-License-Identifier: Apache-2.0

package root

import (
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
)

// TestPauseImageSource runs the original resource-update case in three new
// processes so its subprocesses, daemon, and image store are independent.
func TestPauseImageSource(t *testing.T) {
	const caseName = "TestCrictlUpdateContainerResources"
	for _, trial := range []string{"ordinary", "stock-denied", "declared-denied"} {
		t.Run(trial, func(t *testing.T) {
			var contacts atomic.Int32
			cmd := exec.CommandContext(t.Context(), os.Args[0], "-test.run=^"+caseName+"$", "-test.v")
			cmd.Env = append(os.Environ(), "CRI_PAUSE_TRIAL="+trial)
			if trial != "ordinary" {
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.Method == "CONNECT" && r.Host == "registry.k8s.io:443" {
						contacts.Add(1)
					}
					w.WriteHeader(http.StatusServiceUnavailable)
				}))
				t.Cleanup(server.Close)
				cmd.Env = append(cmd.Env, "HTTPS_PROXY="+server.URL, "https_proxy="+server.URL, "NO_PROXY=", "no_proxy=")
			}
			output, err := cmd.CombinedOutput()
			log := string(output)
			passed := err == nil && strings.Contains(log, "--- PASS: "+caseName+" (")
			ran := strings.Contains(log, "=== RUN   "+caseName)
			pullError := strings.Contains(log, "registry.k8s.io/pause:3.10.1") && (strings.Contains(log, "failed to pull") || strings.Contains(log, "failed pulling") || strings.Contains(log, "failed to get sandbox image"))
			rpcError := strings.Contains(log, "RunPodSandbox") && strings.Contains(log, "rpc error")
			t.Logf("cri-pause trial=%s ran=%t passed=%t pause_pull_error=%t runp_rpc_error=%t registry_connects=%d", trial, ran, passed, pullError, rpcError, contacts.Load())
			switch trial {
			case "stock-denied":
				if !ran || err == nil || !pullError || !rpcError || contacts.Load() == 0 {
					t.Error("expected the first pod to fail at the denied pause-image pull")
				}
			case "declared-denied":
				if !passed || contacts.Load() != 0 {
					t.Error("the resource update must pass with no registry connections")
				}
			case "ordinary":
				if !passed {
					t.Error("the ordinary resource-update case failed")
				}
			}
		})
	}
}
