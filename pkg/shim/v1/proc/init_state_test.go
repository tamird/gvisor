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

package proc

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"sync"
	"testing"

	"github.com/containerd/console"
	"github.com/containerd/containerd/v2/pkg/stdio"
	runc "github.com/containerd/go-runc"
	"gvisor.dev/gvisor/pkg/shim/v1/runsccmd"
)

// fakePlatform is a no-op stdio.Platform.
type fakePlatform struct{}

func (fakePlatform) CopyConsole(ctx context.Context, con console.Console, id, stdin, stdout, stderr string, wg *sync.WaitGroup) (console.Console, error) {
	return con, nil
}

func (fakePlatform) ShutdownConsole(context.Context, console.Console) error { return nil }

func (fakePlatform) Close() error { return nil }

// closerIO records whether it was closed. The embedded interface is nil; only
// the methods below are called.
type closerIO struct {
	runc.IO
	closed bool
}

func (c *closerIO) Close() error {
	c.closed = true
	return nil
}

func (c *closerIO) Set(*exec.Cmd) {}

// TestCreatedStateStartFailureWithoutIO verifies that a failed start does not
// panic when p.io is nil, as Init.Create leaves it for a terminal.
func TestCreatedStateStartFailureWithoutIO(t *testing.T) {
	p := New("id", &runsccmd.Runsc{Command: "/nonexistent-runsc"}, stdio.Stdio{Terminal: true})
	p.Platform = fakePlatform{}
	p.Sandbox = false

	if err := p.Start(t.Context()); err == nil {
		t.Fatal("Start() succeeded, want error")
	}
	if _, ok := p.initState.(*stoppedState); !ok {
		t.Errorf("initState = %T, want *stoppedState", p.initState)
	}
	if got := p.ExitStatus(); got != InternalErrorCode {
		t.Errorf("ExitStatus() = %d, want %d", got, InternalErrorCode)
	}
}

// TestCreatedStateStartFailureClosesIO verifies that the failure path still
// closes the IO when it exists.
func TestCreatedStateStartFailureClosesIO(t *testing.T) {
	p := New("id", &runsccmd.Runsc{Command: "/nonexistent-runsc"}, stdio.Stdio{})
	p.Platform = fakePlatform{}
	p.Sandbox = false
	cio := &closerIO{}
	p.io = cio

	if err := p.Start(t.Context()); err == nil {
		t.Fatal("Start() succeeded, want error")
	}
	if !cio.closed {
		t.Error("process IO was not closed on start failure")
	}
	if _, ok := p.initState.(*stoppedState); !ok {
		t.Errorf("initState = %T, want *stoppedState", p.initState)
	}
}

// TestCreatedStateStartFailureSandbox verifies that a sandbox is left in the
// created state on start failure, so that it can still be deleted.
func TestCreatedStateStartFailureSandbox(t *testing.T) {
	p := New("id", &runsccmd.Runsc{Command: "/nonexistent-runsc"}, stdio.Stdio{Terminal: true})
	p.Platform = fakePlatform{}
	p.Sandbox = true

	if err := p.Start(t.Context()); err == nil {
		t.Fatal("Start() succeeded, want error")
	}
	if _, ok := p.initState.(*createdState); !ok {
		t.Errorf("initState = %T, want *createdState", p.initState)
	}
}

func TestPausedCheckpointExit(t *testing.T) {
	for _, tc := range []struct {
		name         string
		state        string
		leaveRunning bool
		fail         string
		want         string
		wantError    bool
	}{
		{name: "paused exit", state: "paused", want: "state\ncheckpoint\nresume\n"},
		{name: "running exit", state: "running", want: "state\ncheckpoint\n"},
		{name: "paused leave running", state: "paused", leaveRunning: true, want: "checkpoint\n"},
		{name: "state failure", fail: "state", want: "state\n", wantError: true},
		{name: "save failure", state: "paused", fail: "checkpoint", want: "state\ncheckpoint\n", wantError: true},
		{name: "resume failure", state: "paused", fail: "resume", want: "state\ncheckpoint\nresume\n", wantError: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			calls := filepath.Join(dir, "calls")
			command := filepath.Join(dir, "runsc")
			script := "#!/bin/sh\nfor arg do\ncase \"$arg\" in state|checkpoint|resume)\nprintf '%s\\n' \"$arg\" >> " + strconv.Quote(calls) + "\nif [ \"$arg\" = " + strconv.Quote(tc.fail) + " ]; then exit 1; fi\nif [ \"$arg\" = state ]; then printf '%s\\n' " + strconv.Quote("{\"status\":\""+tc.state+"\"}") + "; fi\nexit 0\n;;\nesac\ndone\nexit 1\n"
			if err := os.WriteFile(command, []byte(script), 0o755); err != nil {
				t.Fatal(err)
			}
			p := New("test", &runsccmd.Runsc{Command: command}, stdio.Stdio{})
			p.initState = &runningState{p: p}
			err := p.CheckpointSandbox(t.Context(), &runsccmd.CheckpointOpts{ImagePath: "/image", LeaveRunning: tc.leaveRunning})
			if (err != nil) != tc.wantError {
				t.Fatalf("CheckpointSandbox() = %v, wantError %t", err, tc.wantError)
			}
			got, err := os.ReadFile(calls)
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != tc.want {
				t.Fatalf("runtime calls = %q, want %q", got, tc.want)
			}
		})
	}
}
