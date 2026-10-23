// Copyright 2020 The gVisor Authors.
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

package harness_test

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/pkg/test/testutil"
	"gvisor.dev/gvisor/test/benchmarks/harness"
)

// observingMachine delegates every operation to the real native machine. The
// channel exposes only the actual probe name, without reading its mutable ID.
type observingMachine struct {
	harness.Machine
	created chan *dockerutil.Container
}

func (m *observingMachine) GetNativeContainer(ctx context.Context, logger testutil.Logger) *dockerutil.Container {
	container := m.Machine.GetNativeContainer(ctx, logger)
	fmt.Printf("HTTP_PROBE_OWNED probe %s\n", container.Name)
	m.created <- container
	return container
}

func TestHTTPProbeCancellation(t *testing.T) {
	machine, err := harness.GetMachine()
	if err != nil {
		t.Fatal(err)
	}
	defer machine.CleanUp()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	server := machine.GetNativeContainer(ctx, t)
	fmt.Printf("HTTP_PROBE_OWNED server %s\n", server.Name)
	defer func() {
		cleanup, stop := context.WithTimeout(context.Background(), 30*time.Second)
		defer stop()
		server.CleanUp(cleanup)
	}()
	if err := server.Spawn(ctx, dockerutil.RunOpts{Image: "basic/busybox"}, "sleep", "60"); err != nil {
		t.Fatal(err)
	}
	observed := &observingMachine{Machine: machine, created: make(chan *dockerutil.Container, 1)}
	finished := make(chan struct{})
	var callerErr error
	go func() {
		// The real sleep container deliberately has no HTTP listener.
		callerErr = harness.WaitUntilContainerServing(ctx, observed, server, 80)
		close(finished)
	}()
	var probe *dockerutil.Container
	record := map[string]any{"server": server.Name, "status": "INCOMPLETE"}
	defer func() {
		cancel()
		joined := false
		select {
		case <-finished:
			joined = true
			record["caller_error"] = fmt.Sprint(callerErr)
		case <-time.After(5 * time.Second):
			t.Error("readiness goroutine did not exit during cleanup")
		}
		if probe == nil {
			select {
			case probe = <-observed.created:
			default:
			}
		}
		if probe != nil {
			record["probe"] = probe.Name
		}
		if probe != nil && joined {
			cleanup, stop := context.WithTimeout(context.Background(), 30*time.Second)
			defer stop()
			output, err := exec.CommandContext(cleanup, "docker", "rm", "--force", probe.Name).CombinedOutput()
			record["fallback_cleanup"] = map[string]any{"output": string(output), "error": fmt.Sprint(err)}
			if err != nil && !strings.Contains(string(output), "No such container") {
				t.Errorf("remove owned readiness probe: %v: %s", err, output)
			}
		}
		if t.Failed() {
			record["status"] = "FAIL"
		}
		data, err := json.MarshalIndent(record, "", "  ")
		if err != nil {
			t.Error(err)
			return
		}
		compact, err := json.Marshal(record)
		if err != nil {
			t.Error(err)
			return
		}
		fmt.Printf("HTTP_PROBE_RESULT %s\n", compact)
		if err := os.WriteFile(filepath.Join(os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR"), "http-probe-cancellation.json"), append(data, '\n'), 0644); err != nil {
			t.Error(err)
		}
	}()
	select {
	case probe = <-observed.created:
	case <-ctx.Done():
		t.Fatalf("readiness did not create its actual probe: %v", ctx.Err())
	}
	ready, stopReady := context.WithTimeout(ctx, 10*time.Second)
	defer stopReady()
	if err := testutil.PollContext(ready, func() error {
		output, err := exec.CommandContext(ready, "docker", "inspect", "--format={{.State.Running}}", probe.Name).CombinedOutput()
		if err != nil || strings.TrimSpace(string(output)) != "true" {
			return fmt.Errorf("probe not running: %v: %s", err, output)
		}
		return nil
	}); err != nil {
		t.Fatalf("actual probe did not reach running state: %v", err)
	}
	record["running_before_cancel"] = true
	cancel()
	select {
	case <-finished:
		record["caller_error"] = fmt.Sprint(callerErr)
		if callerErr == nil {
			t.Error("readiness returned success for the unavailable HTTP endpoint")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("readiness did not return after cancellation")
	}
	inspect, stopInspect := context.WithTimeout(context.Background(), 10*time.Second)
	defer stopInspect()
	output, err := exec.CommandContext(inspect, "docker", "ps", "--all", "--quiet", "--no-trunc", "--filter=name=^/"+probe.Name+"$").CombinedOutput()
	record["after_cancel_inventory"] = string(output)
	if err != nil {
		t.Fatalf("inspect readiness cleanup: %v: %s", err, output)
	}
	if strings.TrimSpace(string(output)) != "" {
		record["status"] = "FAIL"
		t.Fatal("readiness left its probe container after cancellation")
	}
	record["status"] = "PASS"
}

func TestMain(m *testing.M) {
	if err := harness.Init(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	os.Exit(m.Run())
}
