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
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/sentry/limits"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/statefile"
	"gvisor.dev/gvisor/pkg/test/testutil"

	_ "gvisor.dev/gvisor/runsc/cli/sentrycli"
)

func TestSudoCallerMappings(t *testing.T) {
	if os.Getuid() != 0 || os.Geteuid() != 0 {
		t.Skip("requires the privileged test runner")
	}
	for _, tc := range []struct {
		name      string
		uid       string
		gid       string
		wantMaps  int
		wantError bool
	}{
		{name: "caller", uid: "1001", gid: "1001", wantMaps: 2},
		{name: "root", uid: "0", gid: "0", wantMaps: 1},
		{name: "negative", uid: "-1", gid: "1001", wantError: true},
		{name: "reserved", uid: "1001", gid: "4294967295", wantError: true},
		{name: "overflow", uid: "4294967296", gid: "1001", wantError: true},
		{name: "invalid", uid: "1001", gid: "bad", wantError: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("SUDO_UID", tc.uid)
			t.Setenv("SUDO_GID", tc.gid)
			attrs := &unix.SysProcAttr{
				UidMappings: []syscall.SysProcIDMap{{ContainerID: 0, HostID: 0, Size: 1}},
				GidMappings: []syscall.SysProcIDMap{{ContainerID: 0, HostID: 0, Size: 1}},
			}
			err := addSudoCallerMappings(attrs)
			if (err != nil) != tc.wantError {
				t.Fatalf("addSudoCallerMappings: %v, wantError=%t", err, tc.wantError)
			}
			if tc.wantError {
				if len(attrs.UidMappings) != 1 || len(attrs.GidMappings) != 1 {
					t.Fatal("invalid input changed namespace mappings")
				}
				return
			}
			if len(attrs.UidMappings) != tc.wantMaps || len(attrs.GidMappings) != tc.wantMaps {
				t.Fatalf("mappings: uid=%v gid=%v, want %d each", attrs.UidMappings, attrs.GidMappings, tc.wantMaps)
			}
			if attrs.UidMappings[0].HostID != 0 || attrs.GidMappings[0].HostID != 0 {
				t.Fatal("root mapping changed")
			}
			if tc.wantMaps == 2 {
				if err := addSudoCallerMappings(attrs); err == nil {
					t.Fatal("duplicate caller mapping accepted")
				}
			}
		})
	}
}

func TestPrintCheckpointDir(t *testing.T) {
	if err := testutil.ConfigureExePath(); err != nil {
		t.Fatalf("ConfigureExePath: %v", err)
	}

	for _, tc := range []struct {
		name        string
		graphEngine bool
	}{
		{name: "graph", graphEngine: true},
		{name: "wire", graphEngine: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			prev := state.GraphEngineEnabled()
			state.EnableGraphEngine(tc.graphEngine)
			defer state.EnableGraphEngine(prev)

			root := t.TempDir()
			validDir := filepath.Join(root, "state.0")
			if err := os.MkdirAll(validDir, 0755); err != nil {
				t.Fatalf("MkdirAll: %v", err)
			}
			f, err := os.Create(filepath.Join(validDir, checkpointFile))
			if err != nil {
				t.Fatalf("os.Create: %v", err)
			}
			w, err := statefile.NewWriter(f, nil, map[string]string{"phase": "autosave"})
			if err != nil {
				f.Close()
				t.Fatalf("statefile.NewWriter: %v", err)
			}
			ls := limits.NewLimitSet()
			ls.SetUnchecked(limits.NumberOfFiles, limits.Limit{Cur: 2048, Max: 8192})
			if _, err := state.Save(context.Background(), w, ls); err != nil {
				w.Close()
				t.Fatalf("state.Save: %v", err)
			}
			if err := w.Close(); err != nil {
				t.Fatalf("writer Close: %v", err)
			}

			emptyDir := filepath.Join(root, "state.1")
			if err := os.MkdirAll(emptyDir, 0755); err != nil {
				t.Fatalf("MkdirAll empty: %v", err)
			}
			ef, err := os.Create(filepath.Join(emptyDir, checkpointFile))
			if err != nil {
				t.Fatalf("os.Create empty: %v", err)
			}
			ef.Close()

			printCheckpointDir(validDir)
			printCheckpointDir(emptyDir)

			txtBytes, err := os.ReadFile(validDir + ".txt")
			if err != nil {
				t.Fatalf("ReadFile(.txt): %v", err)
			}
			txt := string(txtBytes)
			if !strings.Contains(txt, "phase:autosave") || !strings.Contains(txt, "LimitSet") || !strings.Contains(txt, "8192") {
				t.Errorf(".txt output missing expected metadata or state:\n%s", txt)
			}

			htmlBytes, err := os.ReadFile(validDir + ".html")
			if err != nil {
				t.Fatalf("ReadFile(.html): %v", err)
			}
			html := string(htmlBytes)
			if !strings.Contains(html, "LimitSet") || !strings.Contains(html, "8192") {
				t.Errorf(".html output missing expected state:\n%s", html)
			}

			for _, ext := range []string{".txt", ".html"} {
				if _, err := os.Stat(emptyDir + ext); !os.IsNotExist(err) {
					t.Errorf("unexpected %s created for empty checkpoint dir (err=%v)", emptyDir+ext, err)
				}
			}
		})
	}
}
