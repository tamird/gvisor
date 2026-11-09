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

// Package testprocess lets tests run their own binary as a short-lived or
// unresponsive child without depending on a host executable.
package testprocess

import (
	"context"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

// A program selects the behavior of a re-executed test process.
type program string

const (
	// Exit terminates successfully.
	Exit program = "gvisor-test-exit"
	// Pause waits for the caller to terminate the process.
	Pause program = "gvisor-test-pause"
)

// Look at argv[0] before the test binary parses flags or starts its own
// TestMain. A fake runtime receives arguments chosen by the shim.
func init() {
	switch program(filepath.Base(os.Args[0])) {
	case Exit:
		os.Exit(0)
	case Pause:
		for {
			if err := unix.Pause(); err != unix.EINTR {
				log.Fatalf("pause: %v", err)
			}
		}
	}
}

// Command returns an unstarted child. The caller owns its pipes and wait.
func (p program) Command(ctx context.Context) *exec.Cmd {
	cmd := exec.CommandContext(ctx, "/proc/self/exe")
	cmd.Args[0] = string(p)
	cmd.Stderr = os.Stderr
	return cmd
}

// Start returns a running process and its wait result. Test cleanup kills any
// surviving child and joins the same reaper.
func (p program) Start(t *testing.T) (*os.Process, <-chan error) {
	t.Helper()
	cmd := p.Command(t.Context())
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	exited := make(chan error, 1)
	go func() {
		exited <- cmd.Wait()
		close(exited)
	}()
	t.Cleanup(func() {
		cmd.Process.Kill()
		<-exited
	})
	return cmd.Process, exited
}

// Path returns a test-local executable for callers that construct their own
// exec.Cmd and accept only an executable path.
func (p program) Path(t *testing.T) string {
	t.Helper()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), string(p))
	if err := os.Symlink(executable, path); err != nil {
		t.Fatal(err)
	}
	return path
}
