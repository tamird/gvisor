// Copyright 2026 The gVisor Authors.
// SPDX-License-Identifier: Apache-2.0

package root

import (
	"bytes"
	"crypto/sha256"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gvisor.dev/gvisor/pkg/test/testutil"
)

func TestStageDeclaredPause(t *testing.T) {
	// Use the real archive from the standalone target. Stage a small release
	// fixture so a missing archive interrupts launch after it copied a file.
	archive, err := testutil.FindFile("test/root/pause.tar")
	if err != nil {
		t.Fatal(err)
	}
	work := t.TempDir()
	runfiles := filepath.Join(work, "_main")
	for _, dir := range []string{"release", "test/root"} {
		if err := os.MkdirAll(filepath.Join(runfiles, dir), 0755); err != nil {
			t.Fatal(err)
		}
	}
	t.Chdir(runfiles)
	if err := os.WriteFile("release/runtime", []byte("runtime fixture"), 0755); err != nil {
		t.Fatal(err)
	}
	self := filepath.Join(work, "test-binary")
	if err := os.WriteFile(self, []byte("test fixture"), 0755); err != nil {
		t.Fatal(err)
	}

	t.Run("partial_failure", func(t *testing.T) {
		dest := t.TempDir()
		if err := stageRuntime(dest, self); err == nil || !strings.Contains(err.Error(), "locating sandbox image") {
			t.Fatalf("stageRuntime() error = %v, want missing sandbox image", err)
		}
		if contents, err := os.ReadFile(filepath.Join(dest, "runtime")); err != nil || string(contents) != "runtime fixture" {
			t.Fatalf("release file at point of failure = %q, %v", contents, err)
		}
		temp := t.TempDir()
		t.Setenv("TMPDIR", temp)
		if code, err := launch(t.Context(), t); code == 0 || err == nil || !strings.Contains(err.Error(), "locating sandbox image") {
			t.Fatalf("launch() = %d, %v, want missing sandbox image", code, err)
		}
		if entries, err := os.ReadDir(temp); err != nil || len(entries) != 0 {
			t.Fatalf("after failed launch, TMPDIR = %v, %v", entries, err)
		}
		t.Log("the missing archive interrupted staging after the release copy; launch removed its temporary directory")
	})

	if err := os.Symlink(archive, "test/root/pause.tar"); err != nil {
		t.Fatal(err)
	}
	dest := t.TempDir()
	if err := stageRuntime(dest, self); err != nil {
		t.Fatal(err)
	}
	for source, file := range map[string]string{
		"release/runtime": "runtime",
		self:              "test-binary",
		archive:           "pause.tar",
	} {
		want, err := os.ReadFile(source)
		if err != nil {
			t.Fatal(err)
		}
		got, err := os.ReadFile(filepath.Join(dest, file))
		if err != nil || len(got) == 0 || !bytes.Equal(got, want) {
			t.Fatalf("staged %s has %d bytes, want %d, err %v", file, len(got), len(want), err)
		}
		t.Logf("staged %s %d bytes sha256=%x", file, len(got), sha256.Sum256(got))
	}
}
