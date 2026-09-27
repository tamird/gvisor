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

package dockerutil

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/docker/docker/client"

	"gvisor.dev/gvisor/pkg/test/testutil"
	"gvisor.dev/gvisor/runsc/specutils"
)

// StartDaemon starts a private Docker daemon, registers the source-built runsc,
// and loads a declared Docker image archive. It requires root and Docker tools.
// The daemon and its state belong to t; tests using it must not run in parallel.
// Existing dockerutil callers continue using their configured daemon unless
// they explicitly call StartDaemon.
func StartDaemon(t *testing.T, runsc, imageArchive string) {
	t.Helper()
	runsc, err := filepath.Abs(runsc)
	if err != nil {
		t.Fatal(err)
	}
	sidecars := filepath.Join(filepath.Dir(runsc), "gvisor-bin")
	if st, err := os.Stat(sidecars); err != nil || !st.IsDir() {
		t.Fatalf("declared sidecars at %q are unavailable: %v", sidecars, err)
	}
	// Override ambient installations even when FindRunsc was called earlier.
	t.Setenv("GVISOR_SIDECAR_BINARIES_DIR", sidecars)

	// Both dockerd and containerd put Unix sockets below this directory. Bazel's
	// TMPDIR can exceed sockaddr_un's path limit, so keep the owned root short.
	root, err := os.MkdirTemp("/tmp", "gvisor-docker-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		specutils.UnmountNullNetNS(root)
		if err := os.RemoveAll(root); err != nil {
			t.Errorf("remove private Docker state: %v", err)
		}
	})
	// Keep bulk image data in the test workspace, which has a separate disk
	// budget from the worker's small root filesystem.
	dataRoot, err := os.MkdirTemp(testutil.TmpDir(), "docker-data-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(dataRoot); err != nil {
			t.Errorf("remove private Docker image data: %v", err)
		}
	})
	logDir := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if logDir == "" {
		logDir = root
	}
	logs, err := os.Create(filepath.Join(logDir, "dockerd.log"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := logs.Close(); err != nil {
			t.Errorf("close dockerd log: %v", err)
		}
		if t.Failed() {
			output, err := os.ReadFile(logs.Name())
			if err != nil {
				t.Errorf("read dockerd log: %v", err)
			} else {
				t.Logf("dockerd output:\n%s", output)
			}
		}
	})

	host := "unix://" + filepath.Join(root, "docker.sock")
	configPath := filepath.Join(root, "daemon.json")
	cfg, err := json.Marshal(map[string]any{
		"hosts":     []string{host},
		"data-root": dataRoot,
		"exec-root": filepath.Join(root, "exec"),
		"pidfile":   filepath.Join(root, "docker.pid"),
		// The test does not require a particular backing filesystem or systemd.
		"storage-driver":  "vfs",
		"exec-opts":       []string{"native.cgroupdriver=cgroupfs"},
		"default-runtime": "runsc",
		"experimental":    true,
		"runtimes": map[string]any{
			"runsc": map[string]any{
				"path": runsc,
				"runtimeArgs": []string{
					"--platform=systrap",
					// Keep the reusable gofer namespace under fixture ownership.
					"--shared-root=" + root,
					"--allow-suid",
					"--TESTONLY-test-name-env=RUNSC_TEST_NAME",
					"--sidecar-usage-policy=STRICT",
					"--debug",
					"--debug-log=" + filepath.Join(logDir, "runsc.%TIMESTAMP%.%COMMAND%.log"),
				},
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(configPath, cfg, 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DOCKER_HOST", host)
	t.Setenv("DOCKER_TLS_VERIFY", "")
	t.Setenv("DOCKER_CERT_PATH", "")
	t.Setenv("DOCKER_CONTEXT", "")
	t.Setenv("DOCKER_API_VERSION", "")
	oldRuntime, oldConfig := *runtime, *config
	*runtime, *config = "runsc", configPath
	t.Cleanup(func() { *runtime, *config = oldRuntime, oldConfig })

	cmd := exec.Command("dockerd", "--config-file", configPath)
	cmd.Stdout, cmd.Stderr = logs, logs
	// Keep daemon descendants in a separate group so a failed shutdown cannot
	// leave its containerd process behind in the test action.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		t.Fatalf("start dockerd: %v", err)
	}
	done := make(chan struct{})
	var waitErr error
	go func() {
		waitErr = cmd.Wait()
		close(done)
	}()
	// Registered after the directory cleanup: stop and reap before removing state.
	t.Cleanup(func() {
		select {
		case <-done:
			t.Errorf("dockerd exited before cleanup: %v", waitErr)
		default:
			if err := cmd.Process.Signal(syscall.SIGTERM); err != nil && !errors.Is(err, os.ErrProcessDone) {
				t.Errorf("stop dockerd: %v", err)
			}
			select {
			case <-done:
			case <-time.After(15 * time.Second):
				t.Error("dockerd did not stop within 15 seconds")
				if err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
					t.Errorf("kill dockerd process group: %v", err)
				}
				<-done
			}
			if waitErr != nil {
				t.Errorf("wait for dockerd: %v", waitErr)
			}
		}
		// Reap the direct child above even if it failed during startup; kill
		// any descendants left by an abnormal daemon exit as well.
		if err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
			t.Errorf("clean up dockerd process group: %v", err)
		}
	})

	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		t.Fatal(err)
	}
	defer cli.Close()
	ctx, cancel := context.WithTimeout(t.Context(), time.Minute)
	defer cancel()
	tick := time.NewTicker(100 * time.Millisecond)
	defer tick.Stop()
	for {
		pingCtx, pingCancel := context.WithTimeout(ctx, time.Second)
		_, err := cli.Ping(pingCtx)
		pingCancel()
		if err == nil {
			break
		}
		select {
		case <-done:
			t.Fatalf("dockerd exited during startup: %v", waitErr)
		case <-ctx.Done():
			t.Fatalf("wait for dockerd: %v (last ping: %v)", ctx.Err(), err)
		case <-tick.C:
		}
	}
	info, err := cli.Info(ctx)
	if err != nil {
		t.Fatalf("get private daemon info: %v", err)
	}
	if info.DockerRootDir != dataRoot || info.DefaultRuntime != "runsc" {
		t.Fatalf("wrong daemon: data root=%q, default runtime=%q", info.DockerRootDir, info.DefaultRuntime)
	}
	t.Logf("Private Docker %s, storage=%s, cgroup=%s/%s, root=%s; runsc=%s, strict sidecars=%s", info.ServerVersion, info.Driver, info.CgroupDriver, info.CgroupVersion, info.DockerRootDir, runsc, sidecars)

	loadCtx, loadCancel := context.WithTimeout(t.Context(), 2*time.Minute)
	defer loadCancel()
	// The existing Docker CLI handles both HTTP and streamed image-load errors.
	// It only loads this declared archive; there is no registry pull here.
	output, err := exec.CommandContext(loadCtx, dockerCLIPath(), "load", "--input", imageArchive).CombinedOutput()
	if err != nil {
		t.Fatalf("load declared image archive %q: %v\n%s", imageArchive, err, output)
	}
	t.Logf("Loaded declared image archive %s:\n%s", imageArchive, output)
}
