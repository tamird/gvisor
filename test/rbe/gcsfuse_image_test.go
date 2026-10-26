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

package rbe

import (
	"os/exec"
	"runtime"
	"strings"
	"testing"
)

func TestGCSFUSEVersion(t *testing.T) {
	image := "gvisor.dev/images/" + *sourceImage + ":latest"
	for _, args := range [][]string{
		{"image", "inspect", "--format={{.Os}}/{{.Architecture}}", image},
		{"version", "--format={{.Server.Os}}/{{.Server.Arch}}"},
	} {
		output, err := exec.CommandContext(t.Context(), "docker", args...).CombinedOutput()
		if err != nil {
			t.Fatalf("docker %q: %v\n%s", args, err, output)
		}
		if got, want := strings.TrimSpace(string(output)), "linux/"+runtime.GOARCH; got != want {
			t.Fatalf("docker %q platform = %q, want %q", args, got, want)
		}
		t.Logf("Docker %q platform: %s", args, strings.TrimSpace(string(output)))
	}
	output, err := exec.CommandContext(t.Context(), "docker", "run", "--rm",
		"--network=none", "--entrypoint=/usr/local/bin/gcsfuse", image, "--version").CombinedOutput()
	if err != nil {
		t.Fatalf("run packaged gcsfuse: %v\n%s", err, output)
	}
	version := strings.TrimSpace(string(output))
	if want := "gcsfuse version "; !strings.HasPrefix(version, want) {
		t.Fatalf("version output = %q, want prefix %q", version, want)
	}
	t.Logf("Packaged binary: %s", version)
}
