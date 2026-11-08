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

// Package testutil provides fixtures for shim tests.
package testutil

import (
	"log"
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

const wedgedRunsc = "wedged-runsc"

func init() {
	if filepath.Base(os.Args[0]) != wedgedRunsc {
		return
	}
	if err := os.WriteFile(os.Args[0]+".started", nil, 0600); err != nil {
		log.Fatal(err)
	}
	// The shim waits for this process. Only its cancellation should end it.
	os.Stdout.Close()
	os.Stderr.Close()
	for {
		if err := unix.Pause(); err != unix.EINTR {
			log.Fatalf("pause: %v", err)
		}
	}
}

// WedgedRunsc returns a command that accepts any runsc arguments and blocks.
func WedgedRunsc(t *testing.T) string {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), wedgedRunsc)
	if err := os.Symlink(exe, path); err != nil {
		t.Fatal(err)
	}
	return path
}

// CheckStarted distinguishes a timed-out command from an error starting it.
func CheckStarted(t *testing.T, command string) {
	t.Helper()
	if _, err := os.Stat(command + ".started"); err != nil {
		t.Fatalf("wedged runsc did not start: %v", err)
	}
}
