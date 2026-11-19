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

package harness

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
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/docker/docker/api/types/image"
	"github.com/docker/docker/client"
	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

// startDockerDaemon starts a daemon whose storage, socket and child processes
// belong to this test. Its cleanup runs after container and client cleanup.
func startDockerDaemon(t testing.TB, ctx context.Context, runsc string, bridge bool) (string, string, string) {
	t.Helper()
	var releases []string
	if runsc != "" {
		releases = append(releases, runsc)
	}
	host, runtimes, config := startDockerDaemons(t, ctx, releases, bridge)
	selected := "runc"
	if len(runtimes) != 0 {
		selected = runtimes[0]
	}
	return host, selected, config
}

// startDockerDaemons registers each complete release in one owned daemon.
func startDockerDaemons(t testing.TB, ctx context.Context, releases []string, bridge bool) (string, []string, string) {
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
	var parentNet string
	var forwarding []byte
	if bridge {
		parentNet, err = os.Readlink("/proc/self/ns/net")
		if err != nil {
			t.Fatal(err)
		}
		forwarding, err = os.ReadFile("/proc/sys/net/ipv4/ip_forward")
		if err != nil {
			t.Fatal(err)
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
	settings := map[string]any{
		"hosts":     []string{"unix://" + socket},
		"bridge":    "none",
		"iptables":  false,
		"ip6tables": false,
		"data-root": dataRoot,
		"exec-root": execRoot,
		"pidfile":   filepath.Join(root, "docker.pid"),
	}
	if bridge {
		delete(settings, "bridge")
		settings["ip-forward"] = true
	}
	config, err := json.Marshal(settings)
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
	// Keep Docker mounts out of the test's mount namespace. Go marks the
	// child namespace recursively private before exec.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Unshareflags: syscall.CLONE_NEWNS}
	if bridge {
		cmd.SysProcAttr.Unshareflags |= syscall.CLONE_NEWNET
	}
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
		if bridge {
			current, err := os.Readlink("/proc/self/ns/net")
			if err != nil || current != parentNet {
				t.Errorf("parent network namespace changed: %q, %v", current, err)
			}
			currentForwarding, err := os.ReadFile("/proc/sys/net/ipv4/ip_forward")
			if err != nil || string(currentForwarding) != string(forwarding) {
				t.Errorf("parent IP forwarding changed: %q, %v", currentForwarding, err)
			}
			interfaces, err := net.Interfaces()
			if err != nil {
				t.Errorf("list parent interfaces after Docker cleanup: %v", err)
			}
			for _, iface := range interfaces {
				if iface.Name == "docker0" {
					t.Error("Docker bridge appeared in the parent network namespace")
				}
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
			if err := os.RemoveAll(root); err != nil {
				t.Errorf("remove owned Docker files: %v", err)
			} else {
				rootCleaned = true
			}
		}
		if err := json.NewEncoder(os.Stdout).Encode(struct {
			DaemonStopped bool `json:"owned_daemon_stopped"`
			RootCleaned   bool `json:"owned_daemon_files_removed"`
		}{stopped, rootCleaned}); err != nil {
			t.Errorf("write daemon cleanup metadata: %v", err)
		}
	})
	// Each executable resolves its own adjacent opaque sidecars. A daemon-wide
	// override would make distinct runtime releases share the same sidecars.
	env := make([]string, 0, len(os.Environ()))
	for _, value := range os.Environ() {
		if !strings.HasPrefix(value, "GVISOR_SIDECAR_BINARIES_DIR=") {
			env = append(env, value)
		}
	}
	cmd.Env = env
	var selectedRuntimes []string
	for _, runsc := range releases {
		selectedRuntime := "gvisor-worker-" + strings.ToLower(rand.Text())
		runtimeRoot := filepath.Join(root, selectedRuntime)
		if err := os.Mkdir(runtimeRoot, 0700); err != nil {
			t.Fatal(err)
		}
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
		if !ok || entry.Path != runsc || len(installed.Runtimes) != len(selectedRuntimes)+1 || len(entry.Args) != 2 || entry.Args[0] != "--platform=systrap" || entry.Args[1] != "--root="+runtimeRoot {
			t.Fatalf("installed runsc configuration does not match the owned runtime: %+v", installed)
		}
		selectedRuntimes = append(selectedRuntimes, selectedRuntime)
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
	if bridge {
		childNet, err := os.Readlink("/proc/" + strconv.Itoa(cmd.Process.Pid) + "/ns/net")
		if err != nil || childNet == parentNet {
			t.Fatalf("Docker network namespace is not private: %q, %v", childNet, err)
		}
	}
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
				info, err := cli.Info(ctx)
				if err != nil {
					t.Fatalf("inspect installed Docker runtimes: %v", err)
				}
				for i, selected := range selectedRuntimes {
					if entry, ok := info.Runtimes[selected]; !ok || entry.Path != releases[i] {
						t.Fatalf("Docker did not load declared runtime %q", selected)
					}
				}
				return "unix://" + socket, selectedRuntimes, configPath
			}
		}
	}
}

// StartWorker initializes an explicitly selected owned benchmark worker.
// Its cleanup belongs to the top-level benchmark, outside measured subbenchmarks.
func StartWorker(b *testing.B) {
	b.Helper()
	if !*ownedDocker {
		return
	}
	if *workerArchives == "" {
		b.Fatal("owned Docker worker requires declared image archives")
	}
	ctx, cancel := context.WithTimeout(b.Context(), 2*time.Minute)
	defer cancel()
	runsc := ""
	if *workerRunsc != "" {
		path, err := testutil.FindFile(*workerRunsc)
		if err != nil {
			b.Fatal(err)
		}
		path, err = filepath.Abs(path)
		if err != nil {
			b.Fatal(err)
		}
		runsc, err = filepath.EvalSymlinks(path)
		if err != nil {
			b.Fatal(err)
		}
	}
	host, selectedRuntime, configPath := startDockerDaemon(b, ctx, runsc, true)
	b.Setenv("DOCKER_HOST", host)
	// Native benchmark clients must continue to use runc.
	b.Setenv("UNSANDBOXED_RUNTIME", "runc")
	oldRuntime := dockerutil.Runtime()
	dockerutil.SetRuntime(selectedRuntime)
	b.Cleanup(func() { dockerutil.SetRuntime(oldRuntime) })
	oldConfig := flag.Lookup("config_path").Value.String()
	if err := flag.Set("config_path", configPath); err != nil {
		b.Fatal(err)
	}
	b.Cleanup(func() {
		if err := flag.Set("config_path", oldConfig); err != nil {
			b.Error(err)
		}
	})
	cli, err := client.NewClientWithOpts(client.WithHost(host), client.WithAPIVersionNegotiation())
	if err != nil {
		b.Fatal(err)
	}
	defer cli.Close()
	version, err := cli.ServerVersion(ctx)
	if err != nil {
		b.Fatalf("owned Docker server version: %v", err)
	}
	if version.Arch != runtime.GOARCH || version.Os != "linux" {
		b.Fatalf("owned Docker server is %s/%s, want linux/%s", version.Os, version.Arch, runtime.GOARCH)
	}
	parts := strings.Split(version.Version, ".")
	if len(parts) < 3 {
		b.Fatalf("invalid Docker server version %q", version.Version)
	}
	major, majorErr := strconv.Atoi(parts[0])
	minor, minorErr := strconv.Atoi(parts[1])
	if majorErr != nil || minorErr != nil || major < 17 || (major == 17 && minor < 9) {
		b.Fatalf("Docker server version %q is not supported; require at least 17.09", version.Version)
	}
	for _, archive := range strings.Split(*workerArchives, ",") {
		loadDockerImage(b, ctx, cli, archive)
	}
	images, err := cli.ImageList(ctx, image.ListOptions{})
	if err != nil {
		b.Fatalf("list loaded Docker images: %v", err)
	}
	status, err := os.ReadFile("/proc/self/status")
	if err != nil {
		b.Fatal(err)
	}
	allowedCPUs := ""
	for _, line := range strings.Split(string(status), "\n") {
		if value, ok := strings.CutPrefix(line, "Cpus_allowed_list:"); ok {
			allowedCPUs = strings.TrimSpace(value)
			break
		}
	}
	if allowedCPUs == "" {
		b.Fatal("worker process CPU affinity is missing")
	}
	if err := json.NewEncoder(os.Stdout).Encode(struct {
		ServerVersion string          `json:"docker_server_version"`
		Architecture  string          `json:"architecture"`
		Runtime       string          `json:"runtime"`
		Runsc         string          `json:"runsc_entrypoint"`
		CPUs          int             `json:"num_cpu"`
		AllowedCPUs   string          `json:"process_allowed_cpus"`
		ClientCPUs    string          `json:"client_cpuset"`
		ServerCPUs    string          `json:"server_cpuset"`
		Images        []image.Summary `json:"images"`
	}{version.Version, version.Arch, selectedRuntime, runsc, runtime.NumCPU(), allowedCPUs, ClientCPUSet(), ServerCPUSet(), images}); err != nil {
		b.Fatalf("write worker inventory: %v", err)
	}
}

// loadDockerImage loads one declared Docker-save archive into the owned daemon.
func loadDockerImage(t testing.TB, ctx context.Context, cli *client.Client, archive string) {
	t.Helper()
	path, err := testutil.FindFile(archive)
	if err != nil {
		t.Fatal(err)
	}
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	loaded, err := cli.ImageLoad(ctx, f)
	if err != nil {
		t.Fatalf("load declared image %q: %v", archive, err)
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
}
