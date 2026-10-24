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
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPHPCommandGroups(t *testing.T) {
	dir := t.TempDir()
	contents := []struct {
		name    string
		content string
	}{
		{"ordinary.phpt", "--TEST--\nOrdinary test mentions --CAPTURE_STDIO-- in prose\n"},
		{"capture-one.phpt", "--TEST--\nCapture one stream\n--CAPTURE_STDIO--\nSTDOUT\n"},
		{"capture-all.phpt", "--TEST--\r\nCapture all streams\r\n--CAPTURE_STDIO--\r\nSTDIN STDOUT STDERR\r\n"},
		{"ordinary-again.phpt", "--TEST--\nAnother ordinary test\n"},
	}
	var tests []string
	for _, test := range contents {
		name := filepath.Join(dir, test.name)
		if err := os.WriteFile(name, []byte(test.content), 0600); err != nil {
			t.Fatal(err)
		}
		tests = append(tests, name)
	}
	cmds, err := (phpRunner{}).TestCmds(tests)
	if err != nil {
		t.Fatalf("TestCmds: %s", err)
	}
	if got, want := len(cmds), 3; got != want {
		t.Fatalf("command count = %d, want %d", got, want)
	}
	for i, test := range map[int]string{0: tests[0], 2: tests[3]} {
		args := cmds[i].Args
		if args[0] != "make" || args[len(args)-1] != "TESTS=--show-diff "+test {
			t.Errorf("ordinary command %d = %q, want make selecting %q", i, args, test)
		}
	}
	if got := cmds[1].Args[0]; got != "script" {
		t.Errorf("terminal launcher = %q, want script", got)
	}
	var selections []string
	for _, value := range cmds[1].Env {
		if strings.HasPrefix(value, "TESTS=") {
			selections = append(selections, value)
		}
	}
	want := "TESTS=-q --show-diff " + strings.Join(tests[1:3], " ")
	if len(selections) == 0 || selections[len(selections)-1] != want {
		t.Errorf("terminal selection = %q, want final value %q", selections, want)
	}
	if cmds, err := (phpRunner{}).TestCmds(nil); err != nil || len(cmds) != 0 {
		t.Errorf("empty selection = %v, %v; want no commands or error", cmds, err)
	}
}

func TestPHPCommandReadFailure(t *testing.T) {
	dir := t.TempDir()
	name := filepath.Join(dir, "present.phpt")
	if err := os.WriteFile(name, []byte("--TEST--\nOrdinary test\n"), 0600); err != nil {
		t.Fatal(err)
	}
	cmds, err := (phpRunner{}).TestCmds([]string{name, filepath.Join(dir, "missing.phpt")})
	if !errors.Is(err, os.ErrNotExist) {
		t.Errorf("TestCmds error = %v, want file-not-found", err)
	}
	if len(cmds) != 0 {
		t.Errorf("commands after preparation failure = %v, want none", cmds)
	}
}
