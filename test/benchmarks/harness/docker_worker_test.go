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
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/mount"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/client"
	"github.com/docker/docker/errdefs"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

var (
	alpineArchive   = flag.String("alpine-archive", "", "declared Alpine Docker-save archive")
	workerRuntime   = flag.String("worker-runtime", "runc", "runtime under test: runc or runsc")
	runscEntrypoint = flag.String("release-runsc", "", "assembled entrypoint of the declared complete release")
)

func TestDockerWorker(t *testing.T) {
	testDockerWorker(t, false)
}

func TestDockerBridgeWorker(t *testing.T) {
	testDockerWorker(t, true)
}

func testDockerWorker(t *testing.T, bridge bool) {
	t.Helper()
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
	host, selectedRuntime, _ := startDockerDaemon(t, ctx, runsc, bridge)
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
	loadDockerImage(t, ctx, cli, *alpineArchive)
	image, err := cli.ImageInspect(ctx, "gvisor.dev/images/basic/alpine:latest")
	if err != nil {
		t.Fatal(err)
	}
	if image.Architecture != runtime.GOARCH {
		t.Fatalf("loaded image architecture = %q, want %q", image.Architecture, runtime.GOARCH)
	}
	if bridge {
		testDockerBridge(t, ctx, cli, image.ID, selectedRuntime)
		return
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
test -p /shared/fifo
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
	if *workerRuntime == "runc" {
		info, err := os.Lstat(filepath.Join(dir, "fifo"))
		if err != nil || info.Mode()&os.ModeNamedPipe == 0 {
			t.Fatalf("bind-mounted FIFO = %v, %v", info, err)
		}
	}
}

// testDockerBridge checks the named-peer and outbound-IP paths used by the
// benchmark harness. Docker links supply the peer name through /etc/hosts.
func testDockerBridge(t *testing.T, ctx context.Context, cli *client.Client, image, selectedRuntime string) {
	t.Helper()
	const ownerLabel = "gvisor.dev/docker-worker-preflight"
	bridge, err := cli.NetworkInspect(ctx, "bridge", network.InspectOptions{})
	if err != nil || bridge.Driver != "bridge" || len(bridge.Containers) != 0 {
		t.Fatalf("initial Docker bridge = %+v, %v", bridge, err)
	}
	// This cleanup runs after both container cleanups and before client cleanup.
	t.Cleanup(func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		bridge, err := cli.NetworkInspect(cleanupCtx, "bridge", network.InspectOptions{})
		if err != nil || len(bridge.Containers) != 0 {
			t.Errorf("Docker bridge retains endpoints: %+v, %v", bridge.Containers, err)
		}
	})
	create := func(runtime, token, cpus string, links []string) string {
		t.Helper()
		name := "gvisor-bridge-worker-" + strings.ToLower(rand.Text())
		if _, err := cli.ContainerInspect(ctx, name); !errdefs.IsNotFound(err) {
			t.Fatalf("bridge container name is not absent: %v", err)
		}
		t.Cleanup(func() {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			owned, err := cli.ContainerInspect(cleanupCtx, name)
			if errdefs.IsNotFound(err) {
				return
			}
			if err != nil {
				t.Errorf("inspect bridge container: %v", err)
				return
			}
			if owned.Config == nil || owned.Config.Labels[ownerLabel] != name || owned.Image != image || owned.HostConfig == nil || owned.HostConfig.Runtime != runtime {
				t.Error("bridge container ownership changed; refusing cleanup")
				return
			}
			if err := cli.ContainerRemove(cleanupCtx, owned.ID, container.RemoveOptions{Force: true}); err != nil {
				t.Errorf("remove bridge container: %v", err)
				return
			}
			if _, err := cli.ContainerInspect(cleanupCtx, owned.ID); !errdefs.IsNotFound(err) {
				t.Errorf("bridge container remains: %v", err)
			}
		})
		created, err := cli.ContainerCreate(ctx, &container.Config{
			Image: image,
			Env:   []string{"RESPONSE_TOKEN=" + token},
			Cmd: []string{"sh", "-c", `set -eu
command -v nc >/dev/null
command -v wget >/dev/null
while :; do
  printf 'HTTP/1.1 200 OK\r\nContent-Length: %s\r\nConnection: close\r\n\r\n%s\n' "$(( ${#RESPONSE_TOKEN} + 1 ))" "$RESPONSE_TOKEN" | nc -l -p 8080 >/dev/null
done`},
			Labels: map[string]string{ownerLabel: name},
		}, &container.HostConfig{
			Runtime:     runtime,
			NetworkMode: "bridge",
			Links:       links,
			Resources:   container.Resources{CpusetCpus: cpus},
		}, nil, nil, name)
		if err != nil {
			t.Fatalf("create bridge container: %v", err)
		}
		if err := cli.ContainerStart(ctx, created.ID, container.StartOptions{}); err != nil {
			t.Fatalf("start bridge container: %v", err)
		}
		return name
	}
	serverToken, clientToken := rand.Text(), rand.Text()
	if ServerCPUSet() == "" || ClientCPUSet() == "" {
		t.Fatal("bridge worker affinity preflight requires at least four CPUs")
	}
	server := create(selectedRuntime, serverToken, ServerCPUSet(), nil)
	peer := create("runc", clientToken, ClientCPUSet(), []string{server + ":server"})
	serverInfo, err := cli.ContainerInspect(ctx, server)
	if err != nil || serverInfo.NetworkSettings == nil {
		t.Fatalf("inspect selected bridge container: %v", err)
	}
	peerInfo, err := cli.ContainerInspect(ctx, peer)
	if err != nil || peerInfo.NetworkSettings == nil {
		t.Fatalf("inspect native bridge container: %v", err)
	}
	for name, want := range map[string]string{server: ServerCPUSet(), peer: ClientCPUSet()} {
		info, err := cli.ContainerInspect(ctx, name)
		if err != nil || info.State == nil || info.State.Pid == 0 || info.HostConfig == nil || info.HostConfig.CpusetCpus != want {
			t.Fatalf("inspect container CPU assignment: %s, %v", name, err)
		}
		status, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(info.State.Pid), "status"))
		if err != nil {
			t.Fatal(err)
		}
		got := ""
		for _, line := range strings.Split(string(status), "\n") {
			if value, ok := strings.CutPrefix(line, "Cpus_allowed_list:"); ok {
				got = strings.TrimSpace(value)
				break
			}
		}
		if got != want {
			t.Fatalf("container %s effective CPUs = %q, want %q", name, got, want)
		}
		if err := json.NewEncoder(os.Stdout).Encode(struct {
			Runtime   string `json:"affinity_runtime"`
			Image     string `json:"image"`
			Requested string `json:"requested_cpus"`
			Effective string `json:"effective_cpus"`
		}{info.HostConfig.Runtime, info.Image, want, got}); err != nil {
			t.Fatal(err)
		}
	}
	serverIP, peerIP := serverInfo.NetworkSettings.IPAddress, peerInfo.NetworkSettings.IPAddress
	if net.ParseIP(serverIP).To4() == nil || net.ParseIP(peerIP).To4() == nil || serverIP == peerIP {
		t.Fatalf("invalid default-bridge addresses: selected=%q native=%q", serverIP, peerIP)
	}
	bridge, err = cli.NetworkInspect(ctx, "bridge", network.InspectOptions{})
	if err != nil || len(bridge.Containers) != 2 || bridge.Containers[serverInfo.ID].EndpointID == "" || bridge.Containers[peerInfo.ID].EndpointID == "" {
		t.Fatalf("bridge does not contain the two owned endpoints: %+v, %v", bridge.Containers, err)
	}
	fetch := func(from, url, token string) {
		t.Helper()
		for attempt := 0; ; attempt++ {
			output, err := exec.CommandContext(ctx, "docker", "--host="+cli.DaemonHost(), "exec", from, "wget", "-T", "1", "-q", "-O", "-", url).CombinedOutput()
			if err == nil {
				if string(output) != token+"\n" {
					t.Fatalf("bridge HTTP response from %s to %s = %q, want exact peer token", from, url, output)
				}
				return
			}
			info, inspectErr := cli.ContainerInspect(ctx, from)
			if attempt == 29 || inspectErr != nil || info.State == nil || !info.State.Running {
				t.Fatalf("bridge HTTP from %s to %s: %v: %s (inspect: %v)", from, url, err, output, inspectErr)
			}
			time.Sleep(100 * time.Millisecond)
		}
	}
	fetch(peer, "http://server:8080/", serverToken)
	fetch(server, "http://"+peerIP+":8080/", clientToken)
	t.Logf("bridge traffic passed: selected runtime %s, named peer via Docker link, native peer via inspected IPv4", selectedRuntime)
}
