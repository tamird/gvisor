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

package shim_test

import (
	"context"
	"io"
	"os"
	"path/filepath"
	"testing"
	"time"

	task "github.com/containerd/containerd/api/runtime/task/v2"
	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/test/testutil"
	"gvisor.dev/gvisor/shim/shimutils"
)

func TestContainerStdio(t *testing.T) {
	for _, kind := range []string{"standalone", "sandbox", "container"} {
		t.Run(kind, func(t *testing.T) {
			containerd := shimutils.NewMockContainerd(t, nil, nil)
			command := []string{"/bin/sh", "-c", "printf stdout-marker; printf stderr-marker >&2; sleep 100000"}
			spec := shimutils.NewSandboxSpecWithArgs(command...)
			var client task.TaskService
			if kind == "container" {
				parent, parentClient := setupSandbox(t, containerd)
				client = parentClient
				defer func() {
					if err := killAndWaitForContainer(context.Background(), client, parent.ID(), containerd); err != nil {
						t.Error(err)
					}
				}()
				spec = shimutils.NewContainerSpec(parent.ID(), command)
				for i := range spec.Mounts {
					if spec.Mounts[i].Type == "bind" && spec.Mounts[i].Source == testutil.TmpDir() {
						spec.Mounts[i].Source = t.TempDir()
					}
				}
			} else if kind == "standalone" {
				spec.Annotations = nil
			}
			c, err := shimutils.NewContainer(spec, containerd)
			if err != nil {
				t.Fatal(err)
			}
			if client == nil {
				if err := containerd.StartShim(t, c); err != nil {
					t.Fatal(err)
				}
				client = containerd.GetClient(t)
			}
			dir := t.TempDir()
			paths := []string{filepath.Join(dir, "stdout"), filepath.Join(dir, "stderr")}
			streams := make([]*os.File, 0, len(paths))
			for _, path := range paths {
				if err := unix.Mkfifo(path, 0600); err != nil {
					t.Fatal(err)
				}
				stream, err := os.OpenFile(path, os.O_RDWR, 0)
				if err != nil {
					t.Fatal(err)
				}
				defer stream.Close()
				streams = append(streams, stream)
			}
			opts, err := containerd.GetRuntimeOptions()
			if err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
			defer cancel()
			created, err := client.Create(ctx, &task.CreateTaskRequest{ID: c.ID(), Bundle: c.Bundle(), Options: opts, Stdout: paths[0], Stderr: paths[1]})
			if err != nil {
				t.Fatal(err)
			}
			defer func() {
				if err := killAndWaitForContainer(context.Background(), client, c.ID(), containerd); err != nil {
					t.Error(err)
				}
			}()
			if created.Pid == 0 {
				t.Fatal("created task has no live PID")
			}
			if err := startAndWaitForContainer(ctx, client, c.ID(), containerd); err != nil {
				t.Fatal(err)
			}
			for i, want := range []string{"stdout-marker", "stderr-marker"} {
				result := make(chan error, 1)
				buffer := make([]byte, len(want))
				go func() {
					_, err := io.ReadFull(streams[i], buffer)
					result <- err
				}()
				select {
				case err := <-result:
					if err != nil {
						t.Fatal(err)
					}
					if string(buffer) != want {
						t.Fatalf("stream %d = %q, want %q", i, buffer, want)
					}
				case <-ctx.Done():
					t.Fatalf("stream %d did not deliver %q: %v", i, want, ctx.Err())
				}
			}
		})
	}
}
