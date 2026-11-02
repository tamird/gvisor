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

package rbe

import (
	"archive/tar"
	"compress/flate"
	"crypto/sha256"
	"encoding/base64"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

// TestProfileHelperImage exercises the packaged program and the coreutils
// commands used by the Kubernetes profiling DaemonSet.
func TestProfileHelperImage(t *testing.T) {
	ctx := t.Context()
	root := t.TempDir()
	run := func(args ...string) (string, error) {
		opts := []string{"run", "--rm", "--pull=never", "--runtime=runc", "--mount", "type=bind,source=" + root + ",target=/profiles", "gvisor.dev/images/benchmarks/profile-helper:latest"}
		output, err := exec.CommandContext(ctx, "docker", append(opts, args...)...).CombinedOutput()
		return string(output), err
	}
	t.Run("containerd_flags", func(t *testing.T) {
		path := filepath.Join(root, "config.toml")
		const initial = "[runsc_config]\nprofiling = \"off\"\n[next]\nunrelated = true\n"
		if err := os.WriteFile(path, []byte(initial), 0600); err != nil {
			t.Fatal(err)
		}
		for _, step := range []struct{ op, want string }{
			{"set-containerd-flag", "profiling = \"on\""},
			{"remove-containerd-flag", "[runsc_config]\n[next]"},
		} {
			out, err := run("profilehelper", "--operation="+step.op, "--containerd-config=/profiles/config.toml", "--containerd-section=runsc_config", "--flag=profiling", "--value=on")
			if err != nil {
				t.Fatalf("%s: %v: %s", step.op, err, out)
			}
			cfg, err := os.ReadFile(path)
			if err != nil || !strings.Contains(string(cfg), step.want) || !strings.Contains(string(cfg), "unrelated = true") {
				t.Fatalf("%s content %q, read error %v", step.op, cfg, err)
			}
		}
	})
	for _, args := range [][]string{
		{"mkdir", "-p", "/profiles/work/data"},
		{"chmod", "0700", "/profiles/work/data"},
	} {
		if output, err := run(args...); err != nil {
			t.Fatalf("%v: %v\n%s", args, err, output)
		}
	}
	profiles := filepath.Join(root, "work", "data")
	var stat unix.Stat_t
	if err := unix.Stat(profiles, &stat); err != nil {
		t.Fatal(err)
	}
	if got, want := stat.Mode&0777, uint32(0700); got != want {
		t.Errorf("profile directory mode = %#o, want %#o", got, want)
	}
	if got, want := stat.Uid, uint32(0); got != want {
		t.Errorf("profile directory UID = %d, want %d", got, want)
	}
	if got, want := stat.Gid, uint32(0); got != want {
		t.Errorf("profile directory GID = %d, want %d", got, want)
	}
	if err := os.WriteFile(filepath.Join(profiles, "sample"), []byte("declared profile\n"), 0600); err != nil {
		t.Fatal(err)
	}
	output, err := run("profilehelper", "--operation=stream-dir", "--dir=/profiles/work/data")
	if err != nil {
		t.Fatalf("packaged profile helper: %v\n%s", err, output)
	}
	var encoded strings.Builder
	var checksum string
	for line := range strings.SplitSeq(output, "\n") {
		if data, ok := strings.CutPrefix(line, "DATA:"); ok {
			encoded.WriteString(data)
		} else if data, ok := strings.CutPrefix(line, "SHA256:"); ok {
			checksum = data
		}
	}
	if got, want := checksum, fmt.Sprintf("%x", sha256.Sum256([]byte(encoded.String()))); got != want {
		t.Fatalf("stream checksum = %q, want %q\n%s", got, want, output)
	}
	reader := flate.NewReader(base64.NewDecoder(base64.StdEncoding, strings.NewReader(encoded.String())))
	defer reader.Close()
	archive := tar.NewReader(reader)
	header, err := archive.Next()
	if err != nil {
		t.Fatalf("read streamed profile header: %v", err)
	}
	if got, want := header.Name, "sample"; got != want {
		t.Fatalf("profile name = %q, want %q", got, want)
	}
	data, err := io.ReadAll(archive)
	if err != nil {
		t.Fatalf("read streamed profile: %v", err)
	}
	if got, want := string(data), "declared profile\n"; got != want {
		t.Errorf("profile data = %q, want %q", got, want)
	}
	if _, err := archive.Next(); err != io.EOF {
		t.Errorf("archive end = %v, want %v", err, io.EOF)
	}

	if output, err := run("rm", "-rf", "--one-file-system", "/profiles/work"); err != nil {
		t.Fatalf("remove profiles: %v\n%s", err, output)
	}
	if _, err := os.Stat(filepath.Join(root, "work")); !os.IsNotExist(err) {
		t.Fatalf("profile directory after removal: %v, want not-exist", err)
	}

	// A nested mount must survive the exact recursive removal command used by
	// Kubernetes. Removing ordinary siblings proves rm traversed the parent.
	boundary := filepath.Join(root, "boundary")
	protected := filepath.Join(boundary, "mounted")
	if err := os.MkdirAll(protected, 0700); err != nil {
		t.Fatal(err)
	}
	if err := unix.Mount("tmpfs", protected, "tmpfs", unix.MS_NODEV|unix.MS_NOSUID, "size=1m"); err != nil {
		t.Fatalf("mount nested filesystem: %v", err)
	}
	defer func() {
		if err := unix.Unmount(protected, 0); err != nil {
			t.Errorf("unmount nested filesystem: %v", err)
		}
	}()
	var parentStat, mountedStat unix.Stat_t
	if err := unix.Stat(boundary, &parentStat); err != nil {
		t.Fatal(err)
	}
	if err := unix.Stat(protected, &mountedStat); err != nil {
		t.Fatal(err)
	}
	if got, want := mountedStat.Dev, parentStat.Dev; got == want {
		t.Fatalf("nested filesystem device = %d, want different from %d", got, want)
	}
	sentinel := filepath.Join(protected, "sentinel")
	sibling := filepath.Join(boundary, "sibling")
	for _, path := range []string{sentinel, sibling} {
		if err := os.WriteFile(path, []byte("retained mount\n"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	output, err = run("rm", "-rf", "--one-file-system", "/profiles/boundary")
	if err == nil {
		t.Fatalf("removal across a nested mount succeeded; want refusal\n%s", output)
	}
	data, err = os.ReadFile(sentinel)
	if err != nil {
		t.Fatalf("read protected file after removal: %v\n%s", err, output)
	}
	if got, want := string(data), "retained mount\n"; got != want {
		t.Errorf("protected file = %q, want %q", got, want)
	}
	if _, err := os.Stat(sibling); !os.IsNotExist(err) {
		t.Errorf("ordinary sibling after removal: %v, want not-exist", err)
	}
}
