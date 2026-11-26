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

package coverage_test

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

var coverageRuntime = flag.String("coverage-runtime", "", "path to the installed opaque coverage runtime")

// BenchmarkCoverageLifecycle includes process launch, guest execution and exit.
// Archive installation and report inspection are outside the timed interval.
func BenchmarkCoverageLifecycle(b *testing.B) {
	if !filepath.IsAbs(*coverageRuntime) || os.Getenv("GVISOR_SIDECAR_BINARIES_DIR") == "" {
		b.Fatal("an installed runtime and its sidecar directory are required")
	}
	output := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if output == "" {
		b.Fatal("TEST_UNDECLARED_OUTPUTS_DIR is required")
	}
	for _, mode := range []string{"off", "on"} {
		b.Run(mode, func(b *testing.B) {
			b.StopTimer()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				dir := filepath.Join(output, b.Name(), fmt.Sprint(i))
				reports := filepath.Join(dir, "reports")
				if err := os.MkdirAll(reports, 0755); err != nil {
					b.Fatal(err)
				}
				args := []string{"--platform=systrap", "--network=none", "--host-settings=check_mandatory"}
				if mode == "on" {
					args = append(args, "--coverage-report="+reports+"/")
				}
				args = append(args, "do", "--quiet", "/bin/true")
				ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
				cmd := exec.CommandContext(ctx, *coverageRuntime, args...)
				cmd.WaitDelay = 5 * time.Second
				log, err := os.Create(filepath.Join(dir, "command.log"))
				if err != nil {
					cancel()
					b.Fatal(err)
				}
				cmd.Stdout, cmd.Stderr = log, log
				b.Logf("argv=%q", cmd.Args)
				b.StartTimer()
				err = cmd.Run()
				b.StopTimer()
				cancel()
				closeErr := log.Close()
				if err != nil || closeErr != nil {
					b.Fatalf("command: %v; log close: %v", err, closeErr)
				}
				files, err := os.ReadDir(reports)
				if err != nil {
					b.Fatal(err)
				}
				if (len(files) > 0) != (mode == "on") {
					b.Fatalf("report mode %s produced %d files", mode, len(files))
				}
				b.Logf("guest=/bin/true exit=0 mode=%s reports=%d", mode, len(files))
			}
		})
	}
}
