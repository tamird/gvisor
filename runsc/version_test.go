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
	"os"
	"os/exec"
	"strings"
	"testing"
)

// wantVersion is set by Bazel from STABLE_VERSION.
var wantVersion string

func TestVersion(t *testing.T) {
	out, err := exec.Command(os.Getenv("RUNSC"), "--version").Output()
	if err != nil {
		t.Fatalf("runsc --version: %v", err)
	}
	line, _, _ := strings.Cut(string(out), "\n")
	if wantVersion == "" {
		t.Fatalf("STABLE_VERSION is missing, runsc printed %q", line)
	}
	if want := "runsc version " + wantVersion; line != want {
		t.Errorf("runsc --version printed %q, want %q", line, want)
	}
}
