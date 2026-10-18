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

package proctor

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// javaDiagnostics captures the parent jtreg JVM: its default timeout handler
// only inspects the test JVM, which had already exited in the recorded hang.
// This observer is for the fork diagnostic branch, not upstream submission.
func javaDiagnostics(ctx context.Context) {
	start := time.Now()
	for _, offset := range []time.Duration{30 * time.Second, 90 * time.Second, 540 * time.Second} {
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Until(start.Add(offset))):
		}
		func() {
			ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
			defer cancel()
			out, err := exec.CommandContext(ctx, "jcmd", "-l").CombinedOutput()
			fmt.Fprintf(os.Stderr, "JAVA-DIAGNOSTIC %s JVM list: %v\n%s\n", time.Now().UTC(), err, out)
			for line := range strings.SplitSeq(string(out), "\n") {
				if ctx.Err() != nil {
					return
				}
				fields := strings.Fields(line)
				if len(fields) < 2 || strings.Contains(line, "sun.tools.jcmd.JCmd") {
					continue
				}
				pid, err := strconv.Atoi(fields[0])
				if err != nil || pid <= 0 {
					continue
				}
				out, err := exec.CommandContext(ctx, "jcmd", fields[0], "Thread.print", "-l").CombinedOutput()
				fmt.Fprintf(os.Stderr, "JAVA-DIAGNOSTIC PID %d threads: %v\n%s\n", pid, err, out)
				dir := filepath.Join("/proc", fields[0], "fd")
				fds, err := os.ReadDir(dir)
				fmt.Fprintf(os.Stderr, "JAVA-DIAGNOSTIC PID %d descriptors: %v\n", pid, err)
				for _, fd := range fds {
					if ctx.Err() != nil {
						return
					}
					target, err := os.Readlink(filepath.Join(dir, fd.Name()))
					fmt.Fprintf(os.Stderr, "JAVA-DIAGNOSTIC PID %d FD %s -> %s (%v)\n", pid, fd.Name(), target, err)
					if strings.HasPrefix(target, "pipe:") {
						info, err := os.ReadFile(filepath.Join("/proc", fields[0], "fdinfo", fd.Name()))
						fmt.Fprintf(os.Stderr, "JAVA-DIAGNOSTIC PID %d FD %s flags: %v\n%s", pid, fd.Name(), err, info)
					}
				}
			}
		}()
	}
}
