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

// retag_archive changes only the final manifest in a crane Docker archive.
package main

import (
	"archive/tar"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
)

const tarBlockSize = 512

// crane v0.18.0 writes a regular USTAR manifest after all config/layer bodies.
// retag validates that final-entry layout before writing. The archive is a
// private spool; callers must publish it only after success.
// https://github.com/google/go-containerregistry/blob/a0658aa1d/pkg/v1/tarball/write.go#L208-L217
func retag(file *os.File, source, hashTag, latestTag string) (string, error) {
	if source == "" || hashTag == "" || latestTag == "" || hashTag == latestTag {
		return "", fmt.Errorf("expected a source reference and two distinct local tags")
	}
	reader := tar.NewReader(file)
	for {
		header, err := reader.Next()
		if err != nil {
			return "", fmt.Errorf("finding final manifest: %w", err)
		}
		if header.Name != "manifest.json" {
			// Next seeks over unread regular-file bodies when its input is an
			// unbuffered *os.File. Layers are neither copied nor decompressed.
			// https://github.com/golang/go/blob/862c888e6/src/archive/tar/reader.go#L879-L905
			continue
		}
		if header.Typeflag != tar.TypeReg || header.Format != tar.FormatUSTAR || header.Size < 0 || header.Size > 16<<20 {
			return "", fmt.Errorf("expected a regular USTAR manifest of at most 16 MiB")
		}
		dataOffset, err := file.Seek(0, io.SeekCurrent)
		if err != nil {
			return "", err
		}
		// Next has consumed exactly the final header, including any padding
		// before it. Verify that the preceding raw block is that USTAR header.
		headerOffset := dataOffset - tarBlockSize
		var rawHeader [tarBlockSize]byte
		if _, err := file.ReadAt(rawHeader[:], headerOffset); err != nil {
			return "", err
		}
		raw, err := tar.NewReader(bytes.NewReader(rawHeader[:])).Next()
		if err != nil || raw.Name != header.Name || raw.Size != header.Size || raw.Typeflag != tar.TypeReg || raw.Format != tar.FormatUSTAR {
			return "", fmt.Errorf("manifest does not have a standalone USTAR header")
		}
		stat, err := file.Stat()
		if err != nil {
			return "", err
		}
		padding := (tarBlockSize - header.Size%tarBlockSize) % tarBlockSize
		tailOffset := dataOffset + header.Size
		tailSize := padding + 2*tarBlockSize
		if tailOffset < dataOffset || stat.Size()-tailOffset != tailSize {
			return "", fmt.Errorf("manifest must be final, with exactly two zero trailer blocks")
		}
		tail := make([]byte, tailSize)
		if _, err := file.ReadAt(tail, tailOffset); err != nil {
			return "", err
		}
		if !bytes.Equal(tail, make([]byte, len(tail))) {
			return "", fmt.Errorf("manifest padding and trailer must be zero")
		}
		body, err := io.ReadAll(reader)
		if err != nil {
			return "", err
		}
		// Only RepoTags is owned here. Preserve every other manifest field,
		// including metadata unknown to this helper.
		var manifest []map[string]json.RawMessage
		if err := json.Unmarshal(body, &manifest); err != nil {
			return "", fmt.Errorf("decoding manifest: %w", err)
		}
		if len(manifest) != 1 || manifest[0] == nil {
			return "", fmt.Errorf("expected exactly one image in the manifest")
		}
		var originalTags []string
		if err := json.Unmarshal(manifest[0]["RepoTags"], &originalTags); err != nil {
			return "", fmt.Errorf("decoding original tags: %w", err)
		}
		if len(originalTags) != 1 || originalTags[0] != source {
			return "", fmt.Errorf("archive tags are %q, want only %q", originalTags, source)
		}
		var config string
		if err := json.Unmarshal(manifest[0]["Config"], &config); err != nil || config == "" || manifest[0]["Layers"] == nil {
			return "", fmt.Errorf("expected image configuration and layer metadata")
		}
		tags, err := json.Marshal([]string{hashTag, latestTag})
		if err != nil {
			return "", err
		}
		manifest[0]["RepoTags"] = tags
		body, err = json.Marshal(manifest)
		if err != nil {
			return "", err
		}
		if _, err := file.Seek(headerOffset, io.SeekStart); err != nil {
			return "", err
		}
		writer := tar.NewWriter(file)
		header.Size = int64(len(body))
		if err := writer.WriteHeader(header); err != nil {
			return "", err
		}
		if _, err := writer.Write(body); err != nil {
			return "", err
		}
		if err := writer.Close(); err != nil {
			return "", err
		}
		end, err := file.Seek(0, io.SeekCurrent)
		if err != nil {
			return "", err
		}
		return config, file.Truncate(end)
	}
}

func run(path, source, hashTag, latestTag string) error {
	file, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		return err
	}
	config, err := retag(file, source, hashTag, latestTag)
	closeErr := file.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	fmt.Printf("Source config %s; consumer tags %s and %s\n", config, hashTag, latestTag)
	return nil
}

func main() {
	if len(os.Args) != 5 {
		fmt.Fprintln(os.Stderr, "usage: retag_archive ARCHIVE SOURCE HASH_TAG LATEST_TAG")
		os.Exit(2)
	}
	if err := run(os.Args[1], os.Args[2], os.Args[3], os.Args[4]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
