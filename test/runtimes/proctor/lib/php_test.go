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
	"slices"
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
		{"redirect.phpt", "--TEST--\nRedirected tests\n--REDIRECTTEST--\n"},
		{"last.phpt", "--TEST--\nFinal ordinary test\n"},
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
	if got, want := len(cmds), 5; got != want {
		t.Fatalf("command count = %d, want %d", got, want)
	}
	for i, want := range []struct {
		tests              []string
		terminal, redirect string
	}{
		{tests[:1], "false", "false"},
		{tests[1:3], "true", "false"},
		{tests[3:4], "false", "false"},
		{tests[4:5], "false", "true"},
		{tests[5:], "false", "false"},
	} {
		cmd := cmds[i]
		if got := cmd.Args[4:]; !slices.Equal(got, want.tests) {
			t.Errorf("command %d selection = %q, want %q", i, got, want.tests)
		}
		wantEnv := []string{"PHP_TEST_TERMINAL=" + want.terminal, "PHP_TEST_REDIRECT=" + want.redirect}
		if got := cmd.Env[len(cmd.Env)-2:]; !slices.Equal(got, wantEnv) {
			t.Errorf("command %d mode = %q, want %q", i, got, wantEnv)
		}
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
