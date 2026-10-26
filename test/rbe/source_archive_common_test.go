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
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

var sourceArchive = flag.String("archive", "", "Declared Docker-format image archive")
var sourceImage = flag.String("image", "containerd/harness", "Canonical image path in the declared archive")

type sourceArchiveIdentity struct {
	tags     []string
	configID string
	diffIDs  []string
}

// verifySourceArchive checks the archive without extracting or loading it.
func verifySourceArchive(t *testing.T) sourceArchiveIdentity {
	t.Helper()
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
	latest := "gvisor.dev/images/" + *sourceImage + ":latest"
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
	var imageConfig struct {
		RootFS struct {
			DiffIDs []string `json:"diff_ids"`
		}
	}
	if err := json.Unmarshal(config, &imageConfig); err != nil {
		t.Fatal(err)
	}
	configHash := sha256.Sum256(config)
	configID := "sha256:" + hex.EncodeToString(configHash[:])
	var diffIDs []string
	var compressedBytes, unpackedTotal int64
	for _, name := range image.Layers {
		layer, ok := entries[name]
		if !ok {
			t.Fatalf("missing layer %q", name)
		}
		compressedHash := sha256.New()
		compressed := io.TeeReader(io.NewSectionReader(file, layer.offset, layer.size), compressedHash)
		unpacked, err := gzip.NewReader(compressed)
		if err != nil {
			t.Fatalf("layer %q is not the expected compressed layer: %v", name, err)
		}
		plainHash := sha256.New()
		unpackedBytes, err := io.Copy(plainHash, unpacked)
		if err != nil {
			t.Fatal(err)
		}
		if err := unpacked.Close(); err != nil {
			t.Fatal(err)
		}
		if got, want := hex.EncodeToString(compressedHash.Sum(nil)), strings.TrimSuffix(filepath.Base(name), ".tar.gz"); got != want {
			t.Fatalf("layer digest = %q, want filename digest %q", got, want)
		}
		diffIDs = append(diffIDs, "sha256:"+hex.EncodeToString(plainHash.Sum(nil)))
		compressedBytes += layer.size
		unpackedTotal += unpackedBytes
		t.Logf("Validated layer %s: compressed=%d bytes, unpacked=%d bytes", name, layer.size, unpackedBytes)
	}
	if !slices.Equal(diffIDs, imageConfig.RootFS.DiffIDs) {
		t.Fatalf("archive layers = %q, want config layers %q", diffIDs, imageConfig.RootFS.DiffIDs)
	}
	// These are uncompressed tar-stream bytes, not a Docker filesystem size.
	// The load capacity decision must also reserve filesystem and daemon space.
	t.Logf("Validated archive totals: layers=%d, compressed=%d bytes, unpacked=%d bytes", len(diffIDs), compressedBytes, unpackedTotal)
	return sourceArchiveIdentity{tags: image.RepoTags, configID: configID, diffIDs: diffIDs}
}
