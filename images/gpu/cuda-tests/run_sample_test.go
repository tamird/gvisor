// Copyright 2026 The gVisor Authors.
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

package main

import (
	"context"
	"os/exec"
	"testing"
	"time"
)

func TestCommandExitCode(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	command := &Command{Cmd: exec.CommandContext(ctx, "/bin/sh", "-c", "exit 23")}
	if err := command.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if got, err := command.ExitCode(ctx); err != nil || got != 23 {
		t.Fatalf("ExitCode: got (%d, %v), want (23, nil)", got, err)
	}
}
