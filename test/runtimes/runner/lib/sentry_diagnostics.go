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

package lib

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
)

var sentryStacksAfter = flag.Duration("sentry_stacks_after", 0, "diagnostic delay before capturing sentry stacks for a runtime test batch; zero disables capture")

// startSentryDiagnostics supplies separately timed sentry stacks for the Java
// stall investigation. The returned function cancels and joins the capture before
// the runner can remove the container or its artifact directory.
func startSentryDiagnostics(t *testing.T, d *dockerutil.Container, batch int) func() {
	t.Helper()
	if *sentryStacksAfter == 0 {
		return func() {}
	}
	if *sentryStacksAfter < 0 {
		t.Fatal("sentry_stacks_after must be positive or zero")
	}
	isGVisor, err := dockerutil.IsGVisorRuntime(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if !isGVisor {
		return func() {}
	}
	dir := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if dir == "" {
		t.Fatal("sentry stack diagnostics require TEST_UNDECLARED_OUTPUTS_DIR")
	}
	path, err := dockerutil.RuntimePath()
	if err != nil {
		t.Fatal(err)
	}
	root, err := d.RootDirectory()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan struct{})
	go func() {
		defer close(done)
		timer := time.NewTimer(*sentryStacksAfter)
		defer timer.Stop()
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}
		name := filepath.Join(dir, fmt.Sprintf("batch-%d-sentry-stacks.log", batch))
		f, err := os.Create(name)
		if err != nil {
			t.Logf("create sentry diagnostic: %v", err)
			return
		}
		defer f.Close()
		fmt.Fprintf(f, "Started: %s\nContainer: %s\n", time.Now().UTC().Format(time.RFC3339Nano), d.ID())
		ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, path, "--debug-log=/dev/stderr", "--root="+root, "debug", "--stacks", d.ID())
		cmd.WaitDelay = time.Second
		output := &sentryDiagnosticWriter{dst: f, remaining: 8 << 20}
		cmd.Stdout = output
		cmd.Stderr = output
		err = cmd.Run()
		fmt.Fprintf(f, "\nFinished: %s\nCommand: %v\nContext: %v\nOutput: %v\n", time.Now().UTC().Format(time.RFC3339Nano), err, ctx.Err(), output.err)
	}()
	return func() {
		cancel()
		<-done
	}
}

// os/exec serializes writes when Stdout and Stderr share the same writer.
type sentryDiagnosticWriter struct {
	dst       io.Writer
	remaining int
	err       error
}

func (w *sentryDiagnosticWriter) Write(p []byte) (int, error) {
	if len(p) > w.remaining {
		p = p[:w.remaining]
		w.err = fmt.Errorf("sentry stack output exceeds 8 MiB")
	}
	n, err := w.dst.Write(p)
	w.remaining -= n
	if err != nil {
		w.err = err
	}
	return n, w.err
}
