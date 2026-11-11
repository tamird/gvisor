// Copyright 2026 The gVisor Authors.
// SPDX-License-Identifier: Apache-2.0

package root

import (
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
)

func TestPauseImageConfig(t *testing.T) {
	version := os.Getenv("CONTAINERD_VERSION")
	actual, err := exec.CommandContext(t.Context(), getContainerd(), "--version").Output()
	if err != nil || !strings.Contains(string(actual), " "+version+" ") && !strings.Contains(string(actual), " v"+version+" ") {
		t.Fatalf("installed containerd does not match %q", version)
	}
	parts := strings.Split(version, ".")
	major, err := strconv.ParseUint(parts[0], 10, 64)
	if err != nil {
		t.Fatal(err)
	}
	minor, err := strconv.ParseUint(parts[1], 10, 64)
	if err != nil {
		t.Fatal(err)
	}
	const image = "gvisor.dev/images/cri/pause:latest"
	imageLine := regexp.MustCompile("(?m)^\\s*(sandbox_image|sandbox)\\s*=\\s*['\"]" + regexp.QuoteMeta(image) + "['\"]\\s*$")
	for _, entry := range []struct{ name, config string }{
		{"ordinary", getContainerdConfig(major, minor)},
		{"sandbox_controller", getSandboxConfig(major, minor)},
	} {
		t.Run(entry.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "config.toml")
			if err := os.WriteFile(path, []byte(entry.config), 0600); err != nil {
				t.Fatal(err)
			}
			output, err := exec.CommandContext(t.Context(), getContainerd(), "--config", path, "config", "dump").Output()
			if err != nil {
				t.Fatalf("containerd config dump failed: %v", err)
			}
			field := imageLine.FindSubmatch(output)
			if field == nil {
				t.Fatal("the installed containerd did not select the declared sandbox image")
			}
			t.Logf("cri-pause config=%s version=%s field=%s image=%s", entry.name, version, field[1], image)
		})
	}
	t.Run("no_harness", func(t *testing.T) {
		t.Setenv(harnessEnv, "")
		for _, config := range []string{getContainerdConfig(major, minor), getSandboxConfig(major, minor)} {
			if strings.Contains(config, image) {
				t.Fatal("an installed direct test should keep its own sandbox image setting")
			}
		}
		t.Logf("cri-pause version=%s no_harness_preserves_default=true", version)
	})
	var contacts atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "CONNECT" && (r.Host == "registry.k8s.io:443" || r.Host == "gvisor.dev:443") {
			contacts.Add(1)
		}
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	t.Cleanup(proxy.Close)
	for _, key := range []string{"HTTPS_PROXY", "https_proxy"} {
		t.Setenv(key, proxy.URL)
	}
	for _, key := range []string{"NO_PROXY", "no_proxy"} {
		t.Setenv(key, "")
	}
	passed := t.Run("resource_update", TestCrictlUpdateContainerResources)
	t.Logf("cri-pause version=%s resource_update=%t registry_connects=%d", version, passed, contacts.Load())
	if contacts.Load() != 0 {
		t.Error("the daemon contacted the denied pause registry")
	}
}
