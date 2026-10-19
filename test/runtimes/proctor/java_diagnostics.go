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
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// javaDiagnosticOutput bounds each snapshot without silently truncating it.
// The final status line is written separately so an exhausted budget is visible.
type javaDiagnosticOutput struct {
	remaining int
	err       error
}

func (out *javaDiagnosticOutput) Write(p []byte) (int, error) {
	if out.err != nil {
		return 0, out.err
	}
	if len(p) > out.remaining {
		out.err = fmt.Errorf("snapshot exceeds 2 MiB output limit")
		return 0, out.err
	}
	n, err := os.Stderr.Write(p)
	out.remaining -= n
	if err == nil && n != len(p) {
		err = io.ErrShortWrite
	}
	out.err = err
	return n, err
}

// javaPipeHolders records every visible process before jcmd can consume the
// snapshot deadline. Non-JVM children can also keep a test's output pipe open.
// This is a sequential /proc snapshot, not an atomic descriptor census.
func javaPipeHolders(ctx context.Context, out *javaDiagnosticOutput) []string {
	processes, err := os.ReadDir("/proc")
	fmt.Fprintf(out, "JAVA-DIAGNOSTIC %s process census: %v\n", time.Now().UTC(), err)
	var jvms []string
	for _, process := range processes {
		if ctx.Err() != nil || out.err != nil {
			return jvms
		}
		pid, err := strconv.Atoi(process.Name())
		if err != nil || pid <= 0 {
			continue
		}
		dir := filepath.Join("/proc", process.Name())
		status, err := os.ReadFile(filepath.Join(dir, "status"))
		fmt.Fprintf(out, "JAVA-DIAGNOSTIC PID %d identity: %v\n", pid, err)
		for line := range strings.SplitSeq(string(status), "\n") {
			if strings.HasPrefix(line, "Name:") || strings.HasPrefix(line, "Tgid:") || strings.HasPrefix(line, "PPid:") {
				fmt.Fprintf(out, "JAVA-DIAGNOSTIC PID %d %q\n", pid, line)
			}
		}
		fds, err := os.ReadDir(filepath.Join(dir, "fd"))
		fmt.Fprintf(out, "JAVA-DIAGNOSTIC PID %d descriptors: %v\n", pid, err)
		for _, fd := range fds {
			if ctx.Err() != nil || out.err != nil {
				return jvms
			}
			target, err := os.Readlink(filepath.Join(dir, "fd", fd.Name()))
			if err != nil {
				fmt.Fprintf(out, "JAVA-DIAGNOSTIC PID %d FD %s readlink: %v\n", pid, fd.Name(), err)
				continue
			}
			if strings.HasPrefix(target, "pipe:") {
				info, err := os.ReadFile(filepath.Join(dir, "fdinfo", fd.Name()))
				// fdinfo's octal flags retain O_ACCMODE, distinguishing readers
				// from writers without reading pipe contents or process arguments.
				fmt.Fprintf(out, "JAVA-DIAGNOSTIC PID %d FD %s -> %s flags: %v\n%s", pid, fd.Name(), target, err, info)
			}
		}
		executable, err := os.Readlink(filepath.Join(dir, "exe"))
		if err != nil {
			fmt.Fprintf(out, "JAVA-DIAGNOSTIC PID %d executable identity: %v\n", pid, err)
		} else if filepath.Base(executable) == "java" {
			jvms = append(jvms, process.Name())
		}
	}
	return jvms
}

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
			out := &javaDiagnosticOutput{remaining: 2 << 20}
			defer func() {
				fmt.Fprintf(os.Stderr, "JAVA-DIAGNOSTIC snapshot end: context=%v output=%v\n", ctx.Err(), out.err)
			}()
			for _, pid := range javaPipeHolders(ctx, out) {
				if ctx.Err() != nil || out.err != nil {
					return
				}
				fmt.Fprintf(out, "JAVA-DIAGNOSTIC %s PID %s threads begin\n", time.Now().UTC(), pid)
				cmd := exec.CommandContext(ctx, "jcmd", pid, "Thread.print", "-l")
				cmd.Stdout, cmd.Stderr = out, out
				cmd.WaitDelay = time.Second
				err := cmd.Run()
				fmt.Fprintf(out, "JAVA-DIAGNOSTIC PID %s threads end: %v\n", pid, err)
			}
		}()
	}
}
