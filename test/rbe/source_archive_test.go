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

package rbe

import (
	"archive/tar"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"io"
	"os"
	"os/exec"
	"slices"
	"strings"
	"testing"
)

var sourceArchive = flag.String("archive", "", "Declared archive already loaded by the private Docker fixture")

func TestSourceArchiveLoad(t *testing.T) {
	file, err := os.Open(*sourceArchive)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	type entry struct{ offset, size int64 }
	entries := make(map[string]entry)
	var manifest []struct {
		Config   string
		RepoTags []string
		Layers   []string
	}
	reader := tar.NewReader(file)
	for {
		header, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		offset, err := file.Seek(0, io.SeekCurrent)
		if err != nil {
			t.Fatal(err)
		}
		if _, exists := entries[header.Name]; exists {
			t.Fatalf("duplicate archive entry %q", header.Name)
		}
		entries[header.Name] = entry{offset, header.Size}
		if header.Name == "manifest.json" {
			body, err := io.ReadAll(reader)
			if err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal(body, &manifest); err != nil {
				t.Fatal(err)
			}
		}
	}
	if got, want := len(manifest), 1; got != want {
		t.Fatalf("manifest images = %d, want %d", got, want)
	}
	image := manifest[0]
	if got, want := len(image.RepoTags), 2; got != want {
		t.Fatalf("image tags = %q (%d), want %d tags", image.RepoTags, got, want)
	}
	const latest = "gvisor.dev/images/containerd/harness:latest"
	if got, want := image.RepoTags[1], latest; got != want {
		t.Fatalf("latest tag = %q, want %q", got, want)
	}
	configEntry, ok := entries[image.Config]
	if !ok {
		t.Fatalf("missing config %q", image.Config)
	}
	config, err := io.ReadAll(io.NewSectionReader(file, configEntry.offset, configEntry.size))
	if err != nil {
		t.Fatal(err)
	}
	configHash := sha256.Sum256(config)
	configID := "sha256:" + hex.EncodeToString(configHash[:])
	var diffIDs []string
	for _, name := range image.Layers {
		layer, ok := entries[name]
		if !ok {
			t.Fatalf("missing layer %q", name)
		}
		compressedHash := sha256.New()
		compressed := io.TeeReader(io.NewSectionReader(file, layer.offset, layer.size), compressedHash)
		unpacked, err := gzip.NewReader(compressed)
		if err != nil {
			t.Fatalf("layer %q is not the expected compressed crane output: %v", name, err)
		}
		plainHash := sha256.New()
		if _, err := io.Copy(plainHash, unpacked); err != nil {
			t.Fatal(err)
		}
		if err := unpacked.Close(); err != nil {
			t.Fatal(err)
		}
		if got, want := hex.EncodeToString(compressedHash.Sum(nil)), strings.TrimSuffix(name, ".tar.gz"); got != want {
			t.Fatalf("layer digest = %q, want filename digest %q", got, want)
		}
		diffIDs = append(diffIDs, "sha256:"+hex.EncodeToString(plainHash.Sum(nil)))
	}
	args := append([]string{"image", "inspect"}, image.RepoTags...)
	output, err := exec.CommandContext(t.Context(), "docker", args...).CombinedOutput()
	if err != nil {
		t.Fatalf("inspect loaded aliases: %v\n%s", err, output)
	}
	var loaded []struct {
		ID       string `json:"Id"`
		RepoTags []string
		RootFS   struct{ Layers []string }
	}
	if err := json.Unmarshal(output, &loaded); err != nil {
		t.Fatal(err)
	}
	if got, want := len(loaded), 2; got != want {
		t.Fatalf("loaded images = %d, want %d", got, want)
	}
	for i, actual := range loaded {
		if got, want := actual.ID, configID; got != want {
			t.Errorf("tag %q config = %q, want %q", image.RepoTags[i], got, want)
		}
		if !slices.Equal(actual.RootFS.Layers, diffIDs) {
			t.Errorf("tag %q layers = %q, want %q", image.RepoTags[i], actual.RootFS.Layers, diffIDs)
		}
		for _, tag := range image.RepoTags {
			if !slices.Contains(actual.RepoTags, tag) {
				t.Errorf("loaded tags = %q, want %q present", actual.RepoTags, tag)
			}
		}
	}
}
