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
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	eventtypes "github.com/containerd/containerd/api/events"
	task "github.com/containerd/containerd/api/runtime/task/v2"
	runctypes "github.com/containerd/containerd/api/types/runc/options"
	tasktype "github.com/containerd/containerd/api/types/task"
	typeurl "github.com/containerd/typeurl/v2"
	specs "github.com/opencontainers/runtime-spec/specs-go"
	"gvisor.dev/gvisor/pkg/test/testutil"
	"gvisor.dev/gvisor/shim/shimutils"
)

func TestTaskCheckpointRejectsSharedSandbox(t *testing.T) {
	for _, separate := range []bool{false, true} {
		t.Run(fmt.Sprintf("separate-shim=%t", separate), func(t *testing.T) {
			root, cleanup, err := testutil.SetupRootDir()
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(cleanup)
			containerd := shimutils.NewMockContainerdWithSuffix(t, "root", map[string]any{"root": root}, nil)
			sandbox, client := setupSandbox(t, containerd)
			childContainerd, childClient := containerd, client
			if separate {
				childContainerd = shimutils.NewMockContainerdWithSuffix(t, "child", map[string]any{"root": root}, nil)
			}
			child, err := shimutils.NewContainer(shimutils.NewContainerSpec(sandbox.ID(), []string{"sleep", "10000"}), childContainerd)
			if err != nil {
				t.Fatal(err)
			}
			if separate {
				if err := childContainerd.StartShim(t, child); err != nil {
					t.Fatal(err)
				}
				childClient = childContainerd.GetClient(t)
			}
			opts, err := childContainerd.GetRuntimeOptions()
			if err != nil {
				t.Fatal(err)
			}
			if err := createAndWaitForContainer(t.Context(), childClient, child, opts); err != nil {
				t.Fatal(err)
			}
			checkpointOpts, err := typeurl.MarshalAnyToProto(&runctypes.CheckpointOptions{Exit: true})
			if err != nil {
				t.Fatal(err)
			}
			for _, start := range []bool{false, true} {
				wantChild := tasktype.Status_CREATED
				if start {
					if _, err := childClient.Start(t.Context(), &task.StartRequest{ID: child.ID()}); err != nil {
						t.Fatal(err)
					}
					wantChild = tasktype.Status_RUNNING
				}
				for _, target := range []struct {
					id     string
					client task.TaskService
				}{{sandbox.ID(), client}, {child.ID(), childClient}} {
					image := filepath.Join(t.TempDir(), "image")
					if _, err := target.client.Checkpoint(t.Context(), &task.CheckpointTaskRequest{ID: target.id, Path: image, Options: checkpointOpts}); err == nil {
						t.Fatalf("checkpoint of shared sandbox task %s succeeded", target.id)
					}
					if _, err := os.Stat(image); !os.IsNotExist(err) {
						t.Fatalf("rejected checkpoint changed image path: %v", err)
					}
					state, err := client.State(t.Context(), &task.StateRequest{ID: sandbox.ID()})
					if err != nil || state.Status != tasktype.Status_RUNNING {
						t.Fatalf("root after rejected checkpoint: %v, %v", state, err)
					}
					state, err = childClient.State(t.Context(), &task.StateRequest{ID: child.ID()})
					if err != nil || state.Status != wantChild {
						t.Fatalf("child after rejected checkpoint: %v, %v", state, err)
					}
				}
			}
			for _, owner := range []*shimutils.MockContainerd{containerd, childContainerd} {
				for _, event := range owner.Events() {
					if _, ok := event.(*eventtypes.TaskCheckpointed); ok {
						t.Fatal("rejected checkpoint emitted TaskCheckpointed")
					}
				}
			}
			if err := killAndWaitForContainer(t.Context(), childClient, child.ID(), childContainerd); err != nil {
				t.Fatal(err)
			}
			if _, err := childClient.Delete(t.Context(), &task.DeleteRequest{ID: child.ID()}); err != nil {
				t.Fatal(err)
			}
			independent, err := shimutils.NewContainer(shimutils.NewSandboxSpec(), containerd)
			if err != nil {
				t.Fatal(err)
			}
			rootOpts, err := containerd.GetRuntimeOptions()
			if err != nil {
				t.Fatal(err)
			}
			if err := createAndWaitForContainer(t.Context(), client, independent, rootOpts); err != nil {
				t.Fatal(err)
			}
			if _, err := client.Start(t.Context(), &task.StartRequest{ID: independent.ID()}); err != nil {
				t.Fatal(err)
			}
			// Deleting the child makes the root's sandbox exclusive again.
			if _, err := client.Checkpoint(t.Context(), &task.CheckpointTaskRequest{ID: sandbox.ID(), Path: filepath.Join(t.TempDir(), "exclusive"), Options: checkpointOpts}); err != nil {
				t.Fatal(err)
			}
			if err := containerd.WaitForExit(t.Context(), sandbox.ID()); err != nil {
				t.Fatal(err)
			}
			state, err := client.State(t.Context(), &task.StateRequest{ID: independent.ID()})
			if err != nil || state.Status != tasktype.Status_RUNNING {
				t.Fatalf("checkpoint affected independent sandbox: %v, %v", state, err)
			}
			if err := killAndWaitForContainer(t.Context(), client, independent.ID(), containerd); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestTaskCheckpointPausedExit(t *testing.T) {
	for _, exit := range []bool{true, false} {
		t.Run(fmt.Sprintf("exit=%t", exit), func(t *testing.T) {
			control := t.TempDir()
			containerd := shimutils.NewMockContainerd(t, nil, nil)
			spec := shimutils.NewSandboxSpecWithArgs("sh", "-c", "echo ready > /control/ready; while test ! -e /control/advance; do :; done; echo advanced > /control/advanced; sleep 10000")
			spec.Mounts = append(spec.Mounts, specs.Mount{Destination: "/control", Type: "bind", Source: control, Options: []string{"bind"}})
			sandbox, client := setupSandboxWithSpec(t, containerd, spec)
			waitFile := func(name string) {
				t.Helper()
				if err := testutil.Poll(func() error { _, err := os.Stat(filepath.Join(control, name)); return err }, 10*time.Second); err != nil {
					t.Fatalf("guest handshake %s: %v", name, err)
				}
			}
			waitFile("ready")
			if _, err := client.Pause(t.Context(), &task.PauseRequest{ID: sandbox.ID()}); err != nil {
				t.Fatal(err)
			}
			// This command is ready before the snapshot. An unsafe resume would
			// let the old task consume it before completing its exit.
			if err := os.WriteFile(filepath.Join(control, "advance"), nil, 0o600); err != nil {
				t.Fatal(err)
			}
			opts, err := typeurl.MarshalAnyToProto(&runctypes.CheckpointOptions{Exit: exit})
			if err != nil {
				t.Fatal(err)
			}
			image := filepath.Join(t.TempDir(), "image")
			if _, err := client.Checkpoint(t.Context(), &task.CheckpointTaskRequest{ID: sandbox.ID(), Path: image, Options: opts}); err != nil {
				t.Fatal(err)
			}
			if exit {
				ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
				defer cancel()
				if err := containerd.WaitForExit(ctx, sandbox.ID()); err != nil {
					t.Fatalf("checkpoint did not terminate paused task: %v", err)
				}
			} else {
				state, err := client.State(t.Context(), &task.StateRequest{ID: sandbox.ID()})
				if err != nil || state.Status != tasktype.Status_PAUSED {
					t.Fatalf("leave-running checkpoint changed pause state: %v, %v", state, err)
				}
			}
			if _, err := os.Stat(filepath.Join(control, "advanced")); !os.IsNotExist(err) {
				t.Fatalf("guest progressed after paused snapshot: %v", err)
			}
			if exit {
				if _, err := client.Delete(t.Context(), &task.DeleteRequest{ID: sandbox.ID()}); err != nil {
					t.Fatal(err)
				}
				runtimeOpts, err := containerd.GetRuntimeOptions()
				if err != nil {
					t.Fatal(err)
				}
				if _, err := client.Create(t.Context(), &task.CreateTaskRequest{ID: sandbox.ID(), Bundle: sandbox.Bundle(), Options: runtimeOpts, Checkpoint: image}); err != nil {
					t.Fatalf("create task for restore: %v", err)
				}
				if err := startAndWaitForContainer(t.Context(), client, sandbox.ID(), containerd); err != nil {
					t.Fatalf("start restored task: %v", err)
				}
				waitFile("advanced")
				if _, err := client.Checkpoint(t.Context(), &task.CheckpointTaskRequest{ID: sandbox.ID(), Path: filepath.Join(t.TempDir(), "restored"), Options: opts}); err != nil {
					t.Fatalf("checkpoint restored task: %v", err)
				}
				if err := containerd.WaitForExit(t.Context(), sandbox.ID()); err != nil {
					t.Fatal(err)
				}
			}
			if !exit {
				if _, err := client.Resume(t.Context(), &task.ResumeRequest{ID: sandbox.ID()}); err != nil {
					t.Fatal(err)
				}
				waitFile("advanced")
				if err := killAndWaitForContainer(t.Context(), client, sandbox.ID(), containerd); err != nil {
					t.Fatal(err)
				}
			}
		})
	}
}
