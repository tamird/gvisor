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

// Binary stagedcheck checks a staged gzip tarball for the release binaries.
package main

import (
	"archive/tar"
	"compress/gzip"
	_ "embed"
	"fmt"
	"io"
	"maps"
	"os"
	"slices"
	"strings"
)

//go:embed members.txt
var members string

func check(r io.Reader) error {
	missing := make(map[string]struct{})
	for name := range strings.Lines(members) {
		missing[strings.TrimSuffix(name, "\n")] = struct{}{}
	}
	z, err := gzip.NewReader(r)
	if err != nil {
		return err
	}
	t := tar.NewReader(z)
	for {
		h, err := t.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		if h.Typeflag != tar.TypeDir {
			delete(missing, strings.TrimPrefix(h.Name, "./"))
		}
	}
	// The gzip checksum follows the tar end marker.
	if _, err := io.Copy(io.Discard, z); err != nil {
		return err
	}
	if err := z.Close(); err != nil {
		return err
	}
	if len(missing) != 0 {
		return fmt.Errorf("staged archive is missing: %s", strings.Join(slices.Sorted(maps.Keys(missing)), ", "))
	}
	return nil
}

func main() {
	if err := check(os.Stdin); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
