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
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/mount"
	"github.com/docker/docker/client"
	"github.com/docker/docker/errdefs"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

var alpineArchive = flag.String("alpine-archive", "", "declared Alpine Docker-save archive")

func TestDockerWorker(t *testing.T) {
	if *alpineArchive == "" {
		t.Fatal("a declared Alpine archive is required")
	}
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Minute)
	defer cancel()
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
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
			}{versions.Client, version.Version, version.Arch, "runc", cleaned}); err != nil {
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
		if owned.Config == nil || owned.Config.Labels[ownerLabel] != name || owned.Image != image.ID || owned.HostConfig == nil || owned.HostConfig.Runtime != "runc" {
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
		Runtime: "runc",
		Mounts:  []mount.Mount{{Type: mount.TypeBind, Source: dir, Target: "/shared"}},
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
