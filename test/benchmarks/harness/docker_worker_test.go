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

package harness_test

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"flag"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/mount"
	"github.com/docker/docker/client"
	"github.com/docker/docker/errdefs"
	"github.com/moby/sys/mountinfo"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

var (
	alpineArchive   = flag.String("alpine-archive", "", "declared Alpine Docker-save archive")
	workerRuntime   = flag.String("runtime", "runc", "runtime under test: runc or runsc")
	runscEntrypoint = flag.String("release-runsc", "", "assembled entrypoint of the declared complete release")
)

func TestDockerWorker(t *testing.T) {
	if *alpineArchive == "" {
		t.Fatal("a declared Alpine archive is required")
	}
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Minute)
	defer cancel()
	runsc := ""
	switch *workerRuntime {
	case "runc":
		if *runscEntrypoint != "" {
			t.Fatal("native runc worker must not select a runsc release")
		}
	case "runsc":
		if *runscEntrypoint == "" {
			t.Fatal("runsc worker requires a declared release entrypoint")
		}
		path, err := testutil.FindFile(*runscEntrypoint)
		if err != nil {
			t.Fatal(err)
		}
		path, err = filepath.Abs(path)
		if err != nil {
			t.Fatal(err)
		}
		runsc, err = filepath.EvalSymlinks(path)
		if err != nil {
			t.Fatal(err)
		}
	default:
		t.Fatalf("unsupported worker runtime %q", *workerRuntime)
	}
	host, selectedRuntime := startDockerDaemon(t, ctx, runsc)
	cli, err := client.NewClientWithOpts(client.WithHost(host), client.WithAPIVersionNegotiation())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cli.Close() })
	if !strings.HasPrefix(cli.DaemonHost(), "unix://") {
		t.Fatal("Docker worker requires a local Unix-socket daemon")
	}
	version, err := cli.ServerVersion(ctx)
	if err != nil {
		t.Fatalf("Docker server version: %v", err)
	}
	if version.Arch != runtime.GOARCH || version.Os != "linux" {
		t.Fatalf("Docker server is %s/%s, want linux/%s", version.Os, version.Arch, runtime.GOARCH)
	}
	var versions struct {
		Client string `json:"client"`
		Server string `json:"server"`
	}
	output, err := exec.CommandContext(ctx, "docker", "--host="+cli.DaemonHost(), "version", "--format", `{"client":{{json .Client.Version}},"server":{{json .Server.Version}}}`).Output()
	if err != nil {
		t.Fatalf("Docker CLI version: %v", err)
	}
	if err := json.Unmarshal(output, &versions); err != nil {
		t.Fatalf("Docker CLI version output: %v", err)
	}
	if versions.Client == "" || versions.Server != version.Version {
		t.Fatalf("Docker CLI versions = %+v, API server = %q", versions, version.Version)
	}
	archive, err := testutil.FindFile(*alpineArchive)
	if err != nil {
		t.Fatal(err)
	}
	f, err := os.Open(archive)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	loaded, err := cli.ImageLoad(ctx, f)
	if err != nil {
		t.Fatalf("load declared Alpine image: %v", err)
	}
	defer loaded.Body.Close()
	if !loaded.JSON {
		t.Fatal("image load did not return JSON")
	}
	decoder := json.NewDecoder(loaded.Body)
	for {
		var message struct {
			Error       string `json:"error"`
			ErrorDetail *struct {
				Message string `json:"message"`
			} `json:"errorDetail"`
		}
		if err := decoder.Decode(&message); err == io.EOF {
			break
		} else if err != nil {
			t.Fatalf("image-load response: %v", err)
		}
		if message.Error != "" {
			t.Fatalf("image load: %s", message.Error)
		}
		if message.ErrorDetail != nil {
			t.Fatalf("image load: %s", message.ErrorDetail.Message)
		}
	}
	image, err := cli.ImageInspect(ctx, "gvisor.dev/images/basic/alpine:latest")
	if err != nil {
		t.Fatal(err)
	}
	if image.Architecture != runtime.GOARCH {
		t.Fatalf("loaded image architecture = %q, want %q", image.Architecture, runtime.GOARCH)
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "input"), []byte("host\n"), 0600); err != nil {
		t.Fatal(err)
	}
	name := "gvisor-docker-worker-" + strings.ToLower(rand.Text())
	const ownerLabel = "gvisor.dev/docker-worker-preflight"
	if _, err := cli.ContainerInspect(ctx, name); !errdefs.IsNotFound(err) {
		t.Fatalf("container name is not absent: %v", err)
	}
	t.Cleanup(func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		cleaned := false
		defer func() {
			if err := json.NewEncoder(os.Stdout).Encode(struct {
				ClientVersion string `json:"client_version"`
				ServerVersion string `json:"server_version"`
				Architecture  string `json:"architecture"`
				Runtime       string `json:"runtime"`
				Cleanup       bool   `json:"cleanup"`
			}{versions.Client, version.Version, version.Arch, selectedRuntime, cleaned}); err != nil {
				t.Errorf("write worker metadata: %v", err)
			}
		}()
		owned, err := cli.ContainerInspect(cleanupCtx, name)
		if errdefs.IsNotFound(err) {
			cleaned = true
			return
		}
		if err != nil {
			t.Errorf("inspect owned container for cleanup: %v", err)
			return
		}
		if owned.Config == nil || owned.Config.Labels[ownerLabel] != name || owned.Image != image.ID || owned.HostConfig == nil || owned.HostConfig.Runtime != selectedRuntime {
			t.Error("container ownership changed; refusing cleanup")
			return
		}
		if err := cli.ContainerRemove(cleanupCtx, owned.ID, container.RemoveOptions{Force: true}); err != nil {
			t.Errorf("remove owned container: %v", err)
			return
		}
		if _, err := cli.ContainerInspect(cleanupCtx, owned.ID); !errdefs.IsNotFound(err) {
			t.Errorf("owned container remains after cleanup: %v", err)
			return
		}
		cleaned = true
	})
	created, err := cli.ContainerCreate(ctx, &container.Config{
		Image: image.ID,
		Cmd: []string{"sh", "-c", `set -eu
test "$(uname -s)" = Linux
test "$(cat /shared/input)" = host
mkfifo /shared/fifo
printf 'fifo\n' > /shared/fifo &
writer=$!
IFS= read -r message < /shared/fifo
wait "$writer"
test "$message" = fifo
printf 'container\n' > /shared/output`},
		Labels: map[string]string{ownerLabel: name},
	}, &container.HostConfig{
		Runtime:     selectedRuntime,
		NetworkMode: "none",
		Mounts:      []mount.Mount{{Type: mount.TypeBind, Source: dir, Target: "/shared"}},
	}, nil, nil, name)
	if err != nil {
		t.Fatalf("create native container: %v", err)
	}
	if err := cli.ContainerStart(ctx, created.ID, container.StartOptions{}); err != nil {
		t.Fatalf("start native container: %v", err)
	}
	status, errs := cli.ContainerWait(ctx, created.ID, container.WaitConditionNotRunning)
	select {
	case result, ok := <-status:
		if !ok || result.StatusCode != 0 || result.Error != nil {
			logs, logErr := exec.CommandContext(ctx, "docker", "--host="+cli.DaemonHost(), "logs", created.ID).CombinedOutput()
			t.Fatalf("native container exit: %+v (received=%t); logs: %s, %v", result, ok, logs, logErr)
		}
	case err := <-errs:
		t.Fatalf("wait for native container: %v", err)
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	contents, err := os.ReadFile(filepath.Join(dir, "output"))
	if err != nil || string(contents) != "container\n" {
		t.Fatalf("bind-mounted output = %q, %v; want container newline", contents, err)
	}
	info, err := os.Lstat(filepath.Join(dir, "fifo"))
	if err != nil || info.Mode()&os.ModeNamedPipe == 0 {
		t.Fatalf("bind-mounted FIFO = %v, %v", info, err)
	}
}

// startDockerDaemon starts a daemon whose storage, socket and child processes
// belong to this test. Its cleanup runs after container and client cleanup.
func startDockerDaemon(t *testing.T, ctx context.Context, runsc string) (string, string) {
	t.Helper()
	interfaces, err := net.Interfaces()
	if err != nil {
		t.Fatalf("list network interfaces before Docker startup: %v", err)
	}
	for _, iface := range interfaces {
		if iface.Name == "docker0" {
			t.Fatal("fresh Docker worker already has a docker0 bridge")
		}
	}
	root, err := os.MkdirTemp(os.Getenv("TEST_TMPDIR"), "dockerd-")
	if err != nil {
		t.Fatal(err)
	}
	socket := filepath.Join(root, "docker.sock")
	dataRoot := filepath.Join(root, "data")
	execRoot := filepath.Join(root, "e")
	// Docker-managed containerd reserves space for socket suffixes.
	managedSocket := filepath.Join(execRoot, "containerd", "containerd.sock.ttrpc")
	if len(managedSocket) > 104 {
		os.RemoveAll(root)
		t.Fatalf("owned containerd socket path is too long: %d bytes", len(managedSocket))
	}
	if len(socket) >= 108 {
		os.RemoveAll(root)
		t.Fatalf("owned Docker socket path is too long: %d bytes", len(socket))
	}
	configPath := filepath.Join(root, "daemon.json")
	config, err := json.Marshal(map[string]any{
		"hosts":     []string{"unix://" + socket},
		"bridge":    "none",
		"iptables":  false,
		"ip6tables": false,
		"data-root": dataRoot,
		"exec-root": execRoot,
		"pidfile":   filepath.Join(root, "docker.pid"),
	})
	if err != nil {
		os.RemoveAll(root)
		t.Fatal(err)
	}
	if err := os.WriteFile(configPath, config, 0600); err != nil {
		os.RemoveAll(root)
		t.Fatal(err)
	}
	logPath := filepath.Join(root, "daemon.log")
	log, err := os.Create(logPath)
	if err != nil {
		os.RemoveAll(root)
		t.Fatal(err)
	}
	cmd := exec.Command("dockerd", "--config-file="+configPath)
	cmd.Stdout, cmd.Stderr = log, log
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	var exited chan error
	t.Cleanup(func() {
		stopped := true
		if cmd.Process != nil {
			pgid := cmd.Process.Pid
			if err := syscall.Kill(-pgid, syscall.SIGTERM); err != nil && err != syscall.ESRCH {
				t.Errorf("terminate owned Docker group: %v", err)
			}
			deadline := time.Now().Add(10 * time.Second)
			for syscall.Kill(-pgid, 0) == nil && time.Now().Before(deadline) {
				time.Sleep(20 * time.Millisecond)
			}
			if err := syscall.Kill(-pgid, 0); err != syscall.ESRCH {
				if err := syscall.Kill(-pgid, syscall.SIGKILL); err != nil && err != syscall.ESRCH {
					t.Errorf("kill owned Docker group: %v", err)
				}
			}
			select {
			case <-exited:
			case <-time.After(2 * time.Second):
				t.Error("owned Docker leader was not reaped")
				stopped = false
			}
			deadline = time.Now().Add(2 * time.Second)
			for syscall.Kill(-pgid, 0) == nil && time.Now().Before(deadline) {
				time.Sleep(20 * time.Millisecond)
			}
			if err := syscall.Kill(-pgid, 0); err != syscall.ESRCH {
				t.Errorf("owned Docker process group remains: %v", err)
				stopped = false
			}
		}
		if err := log.Close(); err != nil {
			t.Errorf("close Docker log: %v", err)
		}
		if t.Failed() {
			f, err := os.Open(logPath)
			if err == nil {
				defer f.Close()
				if info, err := f.Stat(); err == nil && info.Size() > 8192 {
					f.Seek(-8192, io.SeekEnd)
				}
				data, _ := io.ReadAll(io.LimitReader(f, 8192))
				t.Logf("owned Docker daemon log tail: %s", data)
			}
		}
		rootCleaned := false
		if stopped {
			// Docker marks a data-root self-mount for shutdown cleanup. A
			// startup failure can leave that mount after the process exits.
			marker, markerErr := os.Lstat(filepath.Join(execRoot, "unmount-on-shutdown"))
			var mounted bool
			var mountErr error
			if _, err := os.Lstat(dataRoot); err == nil {
				mounted, mountErr = mountinfo.Mounted(dataRoot)
			} else if !os.IsNotExist(err) {
				mountErr = err
			}
			switch {
			case markerErr != nil && !os.IsNotExist(markerErr):
				t.Errorf("inspect owned Docker unmount marker: %v", markerErr)
			case markerErr == nil && !marker.Mode().IsRegular():
				t.Error("owned Docker unmount marker is not regular")
			case mountErr != nil:
				t.Errorf("inspect owned Docker data-root mount: %v", mountErr)
			case mounted && markerErr != nil:
				t.Error("owned Docker data-root mount has no shutdown marker; retaining files")
			default:
				if mounted {
					unmountErr := syscall.Unmount(dataRoot, 0)
					mounted, mountErr = mountinfo.Mounted(dataRoot)
					if mountErr != nil || mounted {
						t.Errorf("owned Docker data-root remains mounted: unmount=%v, mounted=%t, inspect=%v", unmountErr, mounted, mountErr)
						break
					}
					if unmountErr != nil && unmountErr != syscall.EINVAL {
						t.Errorf("unmount owned Docker data-root: %v", unmountErr)
						break
					}
				}
				if err := os.RemoveAll(root); err != nil {
					t.Errorf("remove owned Docker files: %v", err)
				} else {
					rootCleaned = true
				}
			}
		}
		if err := json.NewEncoder(os.Stdout).Encode(struct {
			DaemonStopped bool `json:"owned_daemon_stopped"`
			RootCleaned   bool `json:"owned_daemon_files_removed"`
		}{stopped, rootCleaned}); err != nil {
			t.Errorf("write daemon cleanup metadata: %v", err)
		}
	})
	selectedRuntime := "runc"
	if runsc != "" {
		selectedRuntime = "gvisor-worker-" + strings.ToLower(rand.Text())
		runtimeRoot := filepath.Join(root, "runsc")
		if err := os.Mkdir(runtimeRoot, 0700); err != nil {
			t.Fatal(err)
		}
		env := make([]string, 0, len(os.Environ())+1)
		for _, value := range os.Environ() {
			if !strings.HasPrefix(value, "GVISOR_SIDECAR_BINARIES_DIR=") {
				env = append(env, value)
			}
		}
		env = append(env, "GVISOR_SIDECAR_BINARIES_DIR="+filepath.Join(filepath.Dir(runsc), "gvisor-bin"))
		cmd.Env = env
		install := exec.CommandContext(ctx, runsc, "install", "--config_file="+configPath,
			"--runtime="+selectedRuntime, "--clobber=false", "--download-sidecars=NEVER",
			"--require-sidecars=ALWAYS", "--", "--platform=systrap", "--root="+runtimeRoot)
		install.Env = env
		if output, err := install.CombinedOutput(); err != nil {
			t.Fatalf("install declared runsc runtime: %v: %s", err, output)
		}
		contents, err := os.ReadFile(configPath)
		if err != nil {
			t.Fatal(err)
		}
		var installed struct {
			Runtimes map[string]struct {
				Path string   `json:"path"`
				Args []string `json:"runtimeArgs"`
			} `json:"runtimes"`
		}
		if err := json.Unmarshal(contents, &installed); err != nil {
			t.Fatal(err)
		}
		entry, ok := installed.Runtimes[selectedRuntime]
		if !ok || entry.Path != runsc || len(installed.Runtimes) != 1 || len(entry.Args) != 2 || entry.Args[0] != "--platform=systrap" || entry.Args[1] != "--root="+runtimeRoot {
			t.Fatalf("installed runsc configuration does not match the owned runtime: %+v", installed)
		}
		t.Cleanup(func() {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			list := exec.CommandContext(cleanupCtx, runsc, "--root="+runtimeRoot, "list", "--format=json")
			list.Env = env
			output, err := list.Output()
			if err != nil {
				t.Errorf("list owned runsc containers after Docker cleanup: %v", err)
				return
			}
			var remaining []json.RawMessage
			if err := json.Unmarshal(output, &remaining); err != nil || len(remaining) != 0 {
				t.Errorf("owned runsc containers remain: %s (%v)", output, err)
				return
			}
			if err := json.NewEncoder(os.Stdout).Encode(struct {
				RuntimeEmpty bool `json:"owned_runsc_empty"`
			}{true}); err != nil {
				t.Errorf("write runsc cleanup metadata: %v", err)
			}
		})
	}
	if err := cmd.Start(); err != nil {
		t.Fatalf("start owned Docker daemon: %v", err)
	}
	exited = make(chan error, 1)
	go func() { exited <- cmd.Wait() }()
	cli, err := client.NewClientWithOpts(client.WithHost("unix://"+socket), client.WithAPIVersionNegotiation())
	if err != nil {
		t.Fatal(err)
	}
	defer cli.Close()
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case err := <-exited:
			exited <- err
			t.Fatalf("owned Docker daemon exited before readiness: %v", err)
		case <-ctx.Done():
			t.Fatalf("owned Docker daemon readiness: %v", ctx.Err())
		case <-ticker.C:
			if _, err := cli.Ping(ctx); err == nil {
				if runsc != "" {
					info, err := cli.Info(ctx)
					if err != nil {
						t.Fatalf("inspect installed Docker runtime: %v", err)
					}
					if entry, ok := info.Runtimes[selectedRuntime]; !ok || entry.Path != runsc {
						t.Fatalf("Docker did not load the declared runtime %q", selectedRuntime)
					}
				}
				return "unix://" + socket, selectedRuntime
			}
		}
	}
}
