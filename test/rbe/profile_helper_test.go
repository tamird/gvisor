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
	"strings"
	"testing"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
)

// TestProfileHelperImage exercises the packaged program and the coreutils
// commands used by the Kubernetes profiling DaemonSet.
func TestProfileHelperImage(t *testing.T) {
	ctx := t.Context()
	container := dockerutil.MakeNativeContainer(ctx, t)
	defer container.CleanUp(ctx)
	output, err := container.Run(ctx, dockerutil.RunOpts{Image: "benchmarks/profile-helper"}, "bash", "-euc", `
mkdir -p /tmp/profiles
chmod 0700 /tmp/profiles
printf 'declared profile\n' > /tmp/profiles/sample
profilehelper --operation=stream-dir --dir=/tmp/profiles
rm -rf --one-file-system /tmp/profiles
test ! -e /tmp/profiles
`)
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
}
