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

package cmd

import (
	"bytes"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"gvisor.dev/gvisor/pkg/test/testutil"
)

func TestStatefileOutputSymlinks(t *testing.T) {
	runsc, err := testutil.FindFile("runsc/runsc")
	if err != nil {
		t.Fatal(err)
	}
	imgDir := t.TempDir()
	img := filepath.Join(imgDir, "meta.img")
	writeSyntheticStatefile(t, img, map[string]string{"k": "v"}, []byte("unused"))

	t.Run("dangling_symlink_creates_target_and_preserves_symlink", func(t *testing.T) {
		targetDir := t.TempDir()
		linkDir := t.TempDir()
		realOut := filepath.Join(targetDir, "dump.txt")
		linkOut := filepath.Join(linkDir, "dump_link.txt")
		if err := os.Symlink(realOut, linkOut); err != nil {
			t.Fatalf("Symlink: %v", err)
		}

		if out, err := exec.Command(runsc, "statefile", "-get=k", "-output="+linkOut, img).CombinedOutput(); err != nil {
			t.Fatalf("statefile -output=<dangling symlink> failed: %v\n%s", err, out)
		}
		linfo, err := os.Lstat(linkOut)
		if err != nil {
			t.Fatalf("Lstat(%s): %v", linkOut, err)
		}
		if linfo.Mode()&os.ModeSymlink == 0 {
			t.Errorf("symlink %s was replaced with mode %v; want symlink preserved", linkOut, linfo.Mode())
		}
		got, err := os.ReadFile(realOut)
		if err != nil {
			t.Errorf("target %s was not created: %v", realOut, err)
		} else if string(got) != "v\n" {
			t.Errorf("target content = %q, want %q", got, "v\n")
		}
	})

	t.Run("symlink_into_nonexistent_dir_fails_and_preserves_symlink", func(t *testing.T) {
		linkDir := t.TempDir()
		brokenLink := filepath.Join(linkDir, "broken_link.txt")
		missingTarget := filepath.Join(t.TempDir(), "no_such_dir", "dump.txt")
		if err := os.Symlink(missingTarget, brokenLink); err != nil {
			t.Fatalf("Symlink: %v", err)
		}

		if out, err := exec.Command(runsc, "statefile", "-get=k", "-output="+brokenLink, img).CombinedOutput(); err == nil {
			t.Errorf("statefile -output=<symlink into missing dir> unexpectedly succeeded:\n%s", out)
		}
		linfo, err := os.Lstat(brokenLink)
		if err != nil {
			t.Fatalf("Lstat(%s): %v", brokenLink, err)
		}
		if linfo.Mode()&os.ModeSymlink == 0 {
			t.Errorf("broken symlink %s was replaced with mode %v; want symlink preserved", brokenLink, linfo.Mode())
		}
	})

	t.Run("dev_stdout_with_unlinked_regular_file", func(t *testing.T) {
		unlinked, err := os.CreateTemp(t.TempDir(), "unlinked-*")
		if err != nil {
			t.Fatalf("CreateTemp: %v", err)
		}
		defer unlinked.Close()
		if err := os.Remove(unlinked.Name()); err != nil {
			t.Fatalf("Remove: %v", err)
		}

		var stderr bytes.Buffer
		cmd := exec.Command(runsc, "statefile", "-get=k", "-output=/dev/stdout", img)
		cmd.Stdout = unlinked
		cmd.Stderr = &stderr
		if err := cmd.Run(); err != nil {
			t.Fatalf("statefile -output=/dev/stdout failed: %v (%s)", err, bytes.TrimSpace(stderr.Bytes()))
		}
		if _, err := unlinked.Seek(0, io.SeekStart); err != nil {
			t.Fatalf("Seek: %v", err)
		}
		got, err := io.ReadAll(unlinked)
		if err != nil {
			t.Fatalf("ReadAll: %v", err)
		}
		if string(got) != "v\n" {
			t.Errorf("stdout content = %q, want %q", got, "v\n")
		}
	})

	t.Run("proc_fd_symlink_in_writable_dir_not_clobbered", func(t *testing.T) {
		unlinked, err := os.CreateTemp(t.TempDir(), "unlinked-*")
		if err != nil {
			t.Fatalf("CreateTemp: %v", err)
		}
		defer unlinked.Close()
		if err := os.Remove(unlinked.Name()); err != nil {
			t.Fatalf("Remove: %v", err)
		}

		stdoutLink := filepath.Join(t.TempDir(), "stdout")
		if err := os.Symlink("/proc/self/fd/1", stdoutLink); err != nil {
			t.Fatalf("Symlink: %v", err)
		}

		var stderr bytes.Buffer
		cmd := exec.Command(runsc, "statefile", "-get=k", "-output="+stdoutLink, img)
		cmd.Stdout = unlinked
		cmd.Stderr = &stderr
		if err := cmd.Run(); err != nil {
			t.Fatalf("statefile -output=%s failed: %v (%s)", stdoutLink, err, bytes.TrimSpace(stderr.Bytes()))
		}
		linfo, err := os.Lstat(stdoutLink)
		if err != nil {
			t.Fatalf("Lstat(%s): %v", stdoutLink, err)
		}
		if linfo.Mode()&os.ModeSymlink == 0 {
			t.Errorf("symlink %s was replaced with mode %v; want symlink preserved", stdoutLink, linfo.Mode())
		}
		if _, err := unlinked.Seek(0, io.SeekStart); err != nil {
			t.Fatalf("Seek: %v", err)
		}
		got, err := io.ReadAll(unlinked)
		if err != nil {
			t.Fatalf("ReadAll: %v", err)
		}
		if string(got) != "v\n" {
			t.Errorf("stdout content = %q, want %q", got, "v\n")
		}
	})

	t.Run("dir_symlink_dotdot_does_not_clobber_input", func(t *testing.T) {
		root := t.TempDir()
		workDir := filepath.Join(root, "work")
		otherChild := filepath.Join(root, "other", "child")
		if err := os.MkdirAll(workDir, 0755); err != nil {
			t.Fatalf("MkdirAll(workDir): %v", err)
		}
		if err := os.MkdirAll(otherChild, 0755); err != nil {
			t.Fatalf("MkdirAll(otherChild): %v", err)
		}
		if err := os.Symlink(filepath.Join("..", "other", "child"), filepath.Join(workDir, "link")); err != nil {
			t.Fatalf("Symlink: %v", err)
		}

		localImg := filepath.Join(workDir, "checkpoint")
		wantInput := writeSyntheticStatefile(t, localImg, map[string]string{"k": "v"}, []byte("unused"))

		outArg := filepath.Join(workDir, "link") + "/../checkpoint"
		if out, err := exec.Command(runsc, "statefile", "-get=k", "-output="+outArg, localImg).CombinedOutput(); err != nil {
			t.Fatalf("statefile -output=%s failed: %v\n%s", outArg, err, out)
		}
		gotInput, err := os.ReadFile(localImg)
		if err != nil {
			t.Fatalf("ReadFile(%s): %v", localImg, err)
		}
		if !bytes.Equal(gotInput, wantInput) {
			t.Errorf("input statefile %s was clobbered", localImg)
		}
		gotOut, err := os.ReadFile(filepath.Join(root, "other", "checkpoint"))
		if err != nil {
			t.Fatalf("ReadFile(other/checkpoint): %v", err)
		}
		if string(gotOut) != "v\n" {
			t.Errorf("other/checkpoint = %q, want %q", gotOut, "v\n")
		}
	})

	t.Run("leaf_symlink_with_relative_dotdot_target", func(t *testing.T) {
		root := t.TempDir()
		workDir := filepath.Join(root, "work")
		realSub := filepath.Join(root, "real_dir", "sub")
		if err := os.MkdirAll(workDir, 0755); err != nil {
			t.Fatalf("MkdirAll(workDir): %v", err)
		}
		if err := os.MkdirAll(realSub, 0755); err != nil {
			t.Fatalf("MkdirAll(realSub): %v", err)
		}
		if err := os.Symlink(realSub, filepath.Join(workDir, "sublink")); err != nil {
			t.Fatalf("Symlink(sublink): %v", err)
		}
		outLink := filepath.Join(workDir, "outlink")
		if err := os.Symlink("sublink/../checkpoint", outLink); err != nil {
			t.Fatalf("Symlink(outlink): %v", err)
		}

		localImg := filepath.Join(workDir, "checkpoint")
		wantInput := writeSyntheticStatefile(t, localImg, map[string]string{"k": "v"}, []byte("unused"))

		if out, err := exec.Command(runsc, "statefile", "-get=k", "-output="+outLink, localImg).CombinedOutput(); err != nil {
			t.Fatalf("statefile -output=%s failed: %v\n%s", outLink, err, out)
		}
		gotInput, err := os.ReadFile(localImg)
		if err != nil {
			t.Fatalf("ReadFile(%s): %v", localImg, err)
		}
		if !bytes.Equal(gotInput, wantInput) {
			t.Errorf("work/checkpoint was clobbered")
		}
		gotOut, err := os.ReadFile(filepath.Join(root, "real_dir", "checkpoint"))
		if err != nil {
			t.Fatalf("ReadFile(real_dir/checkpoint): %v", err)
		}
		if string(gotOut) != "v\n" {
			t.Errorf("real_dir/checkpoint = %q, want %q", gotOut, "v\n")
		}
	})

	t.Run("symlinked_pwd_with_dotdot_output", func(t *testing.T) {
		root := t.TempDir()
		realNested := filepath.Join(root, "real", "nested")
		if err := os.MkdirAll(realNested, 0755); err != nil {
			t.Fatalf("MkdirAll(realNested): %v", err)
		}
		symCWD := filepath.Join(root, "symcwd")
		if err := os.Symlink(realNested, symCWD); err != nil {
			t.Fatalf("Symlink(symCWD): %v", err)
		}
		bystander := filepath.Join(root, "checkpoint")
		wantBystander := []byte("do-not-overwrite")
		if err := os.WriteFile(bystander, wantBystander, 0644); err != nil {
			t.Fatalf("WriteFile(bystander): %v", err)
		}

		cmd := exec.Command(runsc, "statefile", "-get=k", "-output=../checkpoint", img)
		cmd.Dir = symCWD
		env := make([]string, 0, len(os.Environ())+1)
		for _, kv := range os.Environ() {
			if !strings.HasPrefix(kv, "PWD=") {
				env = append(env, kv)
			}
		}
		cmd.Env = append(env, "PWD="+symCWD)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("statefile from symlinked PWD failed: %v\n%s", err, out)
		}

		gotBystander, err := os.ReadFile(bystander)
		if err != nil {
			t.Fatalf("ReadFile(bystander): %v", err)
		}
		if !bytes.Equal(gotBystander, wantBystander) {
			t.Errorf("bystander file clobbered: got %q, want %q", gotBystander, wantBystander)
		}
		gotOut, err := os.ReadFile(filepath.Join(root, "real", "checkpoint"))
		if err != nil {
			t.Fatalf("ReadFile(real/checkpoint): %v", err)
		}
		if string(gotOut) != "v\n" {
			t.Errorf("real/checkpoint = %q, want %q", gotOut, "v\n")
		}
	})

	t.Run("proc_fd_pointing_to_input_rejected_without_corrupting_input", func(t *testing.T) {
		dir := t.TempDir()
		localImg := filepath.Join(dir, "input.img")
		want := writeSyntheticStatefile(t, localImg, map[string]string{"k": "v"}, []byte("payload"))

		rwFile, err := os.OpenFile(localImg, os.O_RDWR, 0)
		if err != nil {
			t.Fatalf("OpenFile: %v", err)
		}
		defer rwFile.Close()

		linkToFD3 := filepath.Join(dir, "fd3_link")
		if err := os.Symlink("/proc/self/fd/3", linkToFD3); err != nil {
			t.Fatalf("Symlink: %v", err)
		}

		for _, outTarget := range []string{"/proc/self/fd/3", linkToFD3} {
			cmd := exec.Command(runsc, "statefile", "-get=k", "-output="+outTarget, localImg)
			cmd.ExtraFiles = []*os.File{rwFile}
			if out, err := cmd.CombinedOutput(); err == nil {
				t.Fatalf("statefile -output=%s unexpectedly succeeded:\n%s", outTarget, out)
			}
			got, err := os.ReadFile(localImg)
			if err != nil {
				t.Fatalf("ReadFile(%s): %v", localImg, err)
			}
			if !bytes.Equal(got, want) {
				t.Errorf("after -output=%s: input clobbered (len=%d, want %d)", outTarget, len(got), len(want))
			}
		}
	})

	t.Run("readonly_existing_file_rejected_when_unprivileged", func(t *testing.T) {
		if os.Geteuid() == 0 {
			t.Skip("root bypasses 0444 DAC write permission check")
		}
		dir := t.TempDir()
		roFile := filepath.Join(dir, "readonly.txt")
		want := []byte("immutable-to-owner")
		if err := os.WriteFile(roFile, want, 0444); err != nil {
			t.Fatalf("WriteFile: %v", err)
		}
		if out, err := exec.Command(runsc, "statefile", "-get=k", "-output="+roFile, img).CombinedOutput(); err == nil {
			t.Fatalf("statefile -output=<0444 file> unexpectedly succeeded:\n%s", out)
		}
		got, err := os.ReadFile(roFile)
		if err != nil {
			t.Fatalf("ReadFile: %v", err)
		}
		if !bytes.Equal(got, want) {
			t.Errorf("readonly file modified: got %q, want %q", got, want)
		}
	})

	t.Run("dirs_without_read_perm_succeed_when_unprivileged", func(t *testing.T) {
		if os.Geteuid() == 0 {
			t.Skip("root bypasses DAC directory read permission check")
		}
		root := t.TempDir()

		wxDir := filepath.Join(root, "wx_only")
		if err := os.Mkdir(wxDir, 0700); err != nil {
			t.Fatalf("Mkdir(wxDir): %v", err)
		}
		t.Cleanup(func() { _ = os.Chmod(wxDir, 0700) })
		if err := os.Chmod(wxDir, 0300); err != nil {
			t.Fatalf("Chmod(wxDir, 0300): %v", err)
		}

		wxOut := filepath.Join(wxDir, "out.txt")
		if out, err := exec.Command(runsc, "statefile", "-get=k", "-output="+wxOut, img).CombinedOutput(); err != nil {
			t.Fatalf("statefile -output in 0300 dir failed: %v\n%s", err, out)
		}
		if got, err := os.ReadFile(wxOut); err != nil {
			t.Fatalf("ReadFile(%s): %v", wxOut, err)
		} else if string(got) != "v\n" {
			t.Errorf("ReadFile(%s) = %q, want %q", wxOut, got, "v\n")
		}

		cwdCmd := exec.Command(runsc, "statefile", "-get=k", "-output=cwd_out.txt", img)
		cwdCmd.Dir = wxDir
		if out, err := cwdCmd.CombinedOutput(); err != nil {
			t.Fatalf("statefile relative -output in 0300 cwd failed: %v\n%s", err, out)
		}
		cwdOut := filepath.Join(wxDir, "cwd_out.txt")
		if got, err := os.ReadFile(cwdOut); err != nil {
			t.Fatalf("ReadFile(%s): %v", cwdOut, err)
		} else if string(got) != "v\n" {
			t.Errorf("ReadFile(%s) = %q, want %q", cwdOut, got, "v\n")
		}

		realDir := filepath.Join(root, "real")
		if err := os.Mkdir(realDir, 0700); err != nil {
			t.Fatalf("Mkdir(realDir): %v", err)
		}
		xDir := filepath.Join(root, "x_only")
		if err := os.Mkdir(xDir, 0700); err != nil {
			t.Fatalf("Mkdir(xDir): %v", err)
		}
		linkPath := filepath.Join(xDir, "outlink")
		targetPath := filepath.Join(realDir, "linked.txt")
		if err := os.Symlink(targetPath, linkPath); err != nil {
			t.Fatalf("Symlink: %v", err)
		}
		t.Cleanup(func() { _ = os.Chmod(xDir, 0700) })
		if err := os.Chmod(xDir, 0100); err != nil {
			t.Fatalf("Chmod(xDir, 0100): %v", err)
		}

		if out, err := exec.Command(runsc, "statefile", "-get=k", "-output="+linkPath, img).CombinedOutput(); err != nil {
			t.Fatalf("statefile -output through 0100 dir symlink failed: %v\n%s", err, out)
		}
		if got, err := os.ReadFile(targetPath); err != nil {
			t.Fatalf("ReadFile(%s): %v", targetPath, err)
		} else if string(got) != "v\n" {
			t.Errorf("ReadFile(%s) = %q, want %q", targetPath, got, "v\n")
		}
	})
}
