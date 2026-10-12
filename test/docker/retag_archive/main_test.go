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

package main

import (
	"archive/tar"
	"bytes"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

const (
	hashTag   = "gvisor.dev/images/sample:0123456789abcdef"
	latestTag = "gvisor.dev/images/sample:latest"
)

func imageArchive(t *testing.T, manifest []byte, format tar.Format, extra bool) ([]byte, int) {
	t.Helper()
	var output bytes.Buffer
	writer := tar.NewWriter(&output)
	write := func(name string, body []byte, format tar.Format) {
		header := &tar.Header{Name: name, Size: int64(len(body)), Mode: 0644, Format: format}
		if format == tar.FormatPAX {
			header.PAXRecords = map[string]string{"comment": "extended manifest"}
		}
		if err := writer.WriteHeader(header); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write(body); err != nil {
			t.Fatal(err)
		}
	}
	write("config.json", []byte(`{"architecture":"amd64"}`), tar.FormatUSTAR)
	write("layer.tar.gz", []byte("opaque compressed layer bytes"), tar.FormatUSTAR)
	if err := writer.Flush(); err != nil {
		t.Fatal(err)
	}
	prefix := output.Len()
	write("manifest.json", manifest, format)
	if extra {
		write("later", []byte("entry after manifest"), tar.FormatUSTAR)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return output.Bytes(), prefix
}

func imageManifest(t *testing.T, source string, padding int) []byte {
	t.Helper()
	body, err := json.Marshal([]map[string]any{{
		"Config": "config.json", "RepoTags": []string{source},
		"Layers": []string{"layer.tar.gz"}, "Unrecognized": map[string]any{"value": 123, "padding": strings.Repeat("x", padding)},
	}})
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func TestRetagPreservesImage(t *testing.T) {
	for _, test := range []struct {
		name    string
		source  string
		padding int
		grows   bool
	}{
		{"grow", "registry.example/image:source", 350, true},
		{"shrink", "registry.example/" + strings.Repeat("long-name-", 20) + "image:source", 200, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			before, prefix := imageArchive(t, imageManifest(t, test.source, test.padding), tar.FormatUSTAR, false)
			path := filepath.Join(t.TempDir(), "image.tar")
			if err := os.WriteFile(path, before, 0600); err != nil {
				t.Fatal(err)
			}
			if err := run(path, test.source, hashTag, latestTag); err != nil {
				t.Fatal(err)
			}
			after, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			if len(after) < prefix {
				t.Fatalf("archive length = %d, want at least prefix length %d", len(after), prefix)
			}
			if !bytes.Equal(after[:prefix], before[:prefix]) {
				t.Error("config/layer prefix changed, want original bytes")
			}
			reader := tar.NewReader(bytes.NewReader(after[prefix:]))
			header, err := reader.Next()
			if err != nil {
				t.Fatal(err)
			}
			if got, want := header.Name, "manifest.json"; got != want {
				t.Fatalf("header.Name = %q, want %q", got, want)
			}
			body, err := io.ReadAll(reader)
			if err != nil {
				t.Fatal(err)
			}
			var got []map[string]any
			if err := json.Unmarshal(body, &got); err != nil {
				t.Fatal(err)
			}
			want := []map[string]any{{
				"Config": "config.json", "RepoTags": []any{hashTag, latestTag},
				"Layers": []any{"layer.tar.gz"}, "Unrecognized": map[string]any{"value": float64(123), "padding": strings.Repeat("x", test.padding)},
			}}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("manifest = %#v, want %#v", got, want)
			}
			if _, err := reader.Next(); err != io.EOF {
				t.Errorf("next header error = %v, want %v", err, io.EOF)
			}
			if got, want := len(after) > len(before), test.grows; got != want || len(after) == len(before) {
				t.Errorf("archive grew = %t (before %d, after %d), want %t", got, len(before), len(after), want)
			}
			wantSize := prefix + tarBlockSize + (len(body)+tarBlockSize-1)/tarBlockSize*tarBlockSize + 2*tarBlockSize
			if got, want := len(after), wantSize; got != want {
				t.Errorf("archive length = %d, want %d", got, want)
			}
		})
	}
}

func TestRejectBeforeWrite(t *testing.T) {
	const source = "registry.example/image:source"
	for _, test := range []struct {
		name     string
		format   tar.Format
		extra    bool
		mutate   func([]byte) []byte
		source   string
		manifest []byte
	}{
		{name: "one_zero_block", mutate: func(b []byte) []byte { return b[:len(b)-tarBlockSize] }},
		{name: "trailing_bytes", mutate: func(b []byte) []byte { return append(b, 0) }},
		{name: "nonzero_trailer", mutate: func(b []byte) []byte { b[len(b)-1] = 1; return b }},
		{name: "not_final", extra: true},
		{name: "gnu_header", format: tar.FormatGNU},
		{name: "pax_header", format: tar.FormatPAX},
		{name: "different_source", source: "registry.example/image:other"},
		{name: "malformed_json", manifest: []byte(`{`)},
		{name: "two_images", manifest: []byte(`[{},{}]`)},
	} {
		t.Run(test.name, func(t *testing.T) {
			format := test.format
			if format == tar.FormatUnknown {
				format = tar.FormatUSTAR
			}
			manifest := test.manifest
			if manifest == nil {
				manifest = imageManifest(t, source, 0)
			}
			before, _ := imageArchive(t, manifest, format, test.extra)
			if test.mutate != nil {
				before = test.mutate(before)
			}
			path := filepath.Join(t.TempDir(), "image.tar")
			if err := os.WriteFile(path, before, 0600); err != nil {
				t.Fatal(err)
			}
			expectedSource := source
			if test.source != "" {
				expectedSource = test.source
			}
			if err := run(path, expectedSource, hashTag, latestTag); err == nil {
				t.Fatal("retag succeeded, want rejection before writing")
			}
			after, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(after, before) {
				t.Error("rejected archive changed, want original bytes")
			}
		})
	}
}
