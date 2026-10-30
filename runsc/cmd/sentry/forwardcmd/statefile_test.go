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

package forwardcmd

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

func TestCheckStickyDirOwnership(t *testing.T) {
	stickyWorld := &unix.Stat_t{Mode: unix.S_IFDIR | unix.S_ISVTX | 0777, Uid: 0}
	stickyNotWorld := &unix.Stat_t{Mode: unix.S_IFDIR | unix.S_ISVTX | 0775, Uid: 0}
	nonStickyWorld := &unix.Stat_t{Mode: unix.S_IFDIR | 0777, Uid: 0}

	if err := checkStickyDirOwnership(stickyWorld, 1000, 0); !errors.Is(err, unix.EACCES) {
		t.Errorf("root following uid=1000 entry in root-owned 1777 dir: got %v, want EACCES", err)
	}
	if err := checkStickyDirOwnership(stickyWorld, 1000, 2000); !errors.Is(err, unix.EACCES) {
		t.Errorf("uid=2000 following uid=1000 entry in root-owned 1777 dir: got %v, want EACCES", err)
	}
	if err := checkStickyDirOwnership(stickyWorld, 1000, 1000); err != nil {
		t.Errorf("owner following own entry in 1777 dir: got %v, want nil", err)
	}
	if err := checkStickyDirOwnership(stickyWorld, 0, 1000); err != nil {
		t.Errorf("following dir-owner entry in 1777 dir: got %v, want nil", err)
	}
	if err := checkStickyDirOwnership(stickyNotWorld, 1000, 0); err != nil {
		t.Errorf("following entry in non-world-writable sticky dir: got %v, want nil", err)
	}
	if err := checkStickyDirOwnership(nonStickyWorld, 1000, 0); err != nil {
		t.Errorf("following entry in non-sticky dir: got %v, want nil", err)
	}
}

func TestSplitParentAndBase(t *testing.T) {
	for _, tc := range []struct {
		in            string
		wantParent    string
		wantBase      string
		wantHasParent bool
		wantTrailing  bool
	}{
		{in: "file.txt", wantParent: "", wantBase: "file.txt", wantHasParent: false, wantTrailing: false},
		{in: "/file.txt", wantParent: "/", wantBase: "file.txt", wantHasParent: true, wantTrailing: false},
		{in: "link/../checkpoint", wantParent: "link/..", wantBase: "checkpoint", wantHasParent: true, wantTrailing: false},
		{in: "../checkpoint", wantParent: "..", wantBase: "checkpoint", wantHasParent: true, wantTrailing: false},
		{in: "dir/", wantParent: "", wantBase: "", wantHasParent: false, wantTrailing: true},
		{in: "/", wantParent: "", wantBase: "", wantHasParent: false, wantTrailing: true},
	} {
		parent, base, hasParent, trailing := splitParentAndBase(tc.in)
		if parent != tc.wantParent || base != tc.wantBase || hasParent != tc.wantHasParent || trailing != tc.wantTrailing {
			t.Errorf("splitParentAndBase(%q) = (%q, %q, %v, %v), want (%q, %q, %v, %v)",
				tc.in, parent, base, hasParent, trailing,
				tc.wantParent, tc.wantBase, tc.wantHasParent, tc.wantTrailing)
		}
	}
}

func TestWriteOutputFileSymlinkDotDotTraversal(t *testing.T) {
	root := t.TempDir()
	workDir := filepath.Join(root, "work")
	otherChild := filepath.Join(root, "other", "child")
	if err := os.MkdirAll(workDir, 0755); err != nil {
		t.Fatalf("MkdirAll(workDir): %v", err)
	}
	if err := os.MkdirAll(otherChild, 0755); err != nil {
		t.Fatalf("MkdirAll(otherChild): %v", err)
	}
	// work/link -> ../other/child, so work/link/../checkpoint resolves in the
	// kernel to root/other/checkpoint, NOT work/checkpoint.
	if err := os.Symlink(filepath.Join("..", "other", "child"), filepath.Join(workDir, "link")); err != nil {
		t.Fatalf("Symlink: %v", err)
	}

	inputPath := filepath.Join(workDir, "checkpoint")
	wantInput := []byte("original-input-checkpoint")
	if err := os.WriteFile(inputPath, wantInput, 0644); err != nil {
		t.Fatalf("WriteFile(input): %v", err)
	}
	inFile, err := os.Open(inputPath)
	if err != nil {
		t.Fatalf("Open(input): %v", err)
	}
	defer inFile.Close()
	var inStat unix.Stat_t
	if err := unix.Fstat(int(inFile.Fd()), &inStat); err != nil {
		t.Fatalf("Fstat(input): %v", err)
	}

	outExpr := filepath.Join(workDir, "link") + "/../checkpoint"
	if err := writeOutputFile(outExpr, &inStat, func(f *os.File) error {
		_, werr := f.WriteString("decoded-output\n")
		return werr
	}); err != nil {
		t.Fatalf("writeOutputFile(%q) failed: %v", outExpr, err)
	}

	gotInput, err := os.ReadFile(inputPath)
	if err != nil {
		t.Fatalf("ReadFile(input): %v", err)
	}
	if string(gotInput) != string(wantInput) {
		t.Errorf("input file %s was clobbered: got %q, want %q", inputPath, gotInput, wantInput)
	}

	resolvedOut := filepath.Join(root, "other", "checkpoint")
	gotOut, err := os.ReadFile(resolvedOut)
	if err != nil {
		t.Fatalf("ReadFile(resolvedOut): %v", err)
	}
	if string(gotOut) != "decoded-output\n" {
		t.Errorf("resolved output %s = %q, want %q", resolvedOut, gotOut, "decoded-output\n")
	}
}
