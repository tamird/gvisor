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
	"context"
	"io"
	"maps"
	"net/http"
	"strings"
	"testing"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func TestTagCommits(t *testing.T) {
	const tag = "0123456789abcdef0123456789abcdef01234567"
	const commit = "abcdef0123456789abcdef0123456789abcdef01"
	for _, tc := range []struct {
		name          string
		advertisement string
		want          map[string]string
		wantError     bool
	}{
		{
			name:          "lightweight",
			advertisement: commit + "\trefs/tags/550.54.14\n",
			want:          map[string]string{"550.54.14": commit},
		},
		{
			name:          "annotated_tag_is_peeled",
			advertisement: tag + "\trefs/tags/550.54.14\n" + commit + "\trefs/tags/550.54.14^{}\n",
			want:          map[string]string{"550.54.14": commit},
		},
		{
			name:          "peeled_ref_before_tag",
			advertisement: commit + "\trefs/tags/550.54.14^{}\n" + tag + "\trefs/tags/550.54.14\n",
			want:          map[string]string{"550.54.14": commit},
		},
		{
			name: "no_advertised_tags",
			want: map[string]string{},
		},
		{
			name:          "missing_tag_for_peeled_ref",
			advertisement: commit + "\trefs/tags/550.54.14^{}\n",
			wantError:     true,
		},
		{
			name:          "malformed_object",
			advertisement: "not-a-commit\trefs/tags/550.54.14\n",
			wantError:     true,
		},
		{
			name:          "non_tag_ref",
			advertisement: commit + "\trefs/heads/550.54.14\n",
			wantError:     true,
		},
		{
			name:          "truncated_advertisement",
			advertisement: commit,
			wantError:     true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := tagCommits([]byte(tc.advertisement))
			if (err != nil) != tc.wantError {
				t.Fatalf("tagCommits error = %v, wantError = %t", err, tc.wantError)
			}
			if err == nil && !maps.Equal(got, tc.want) {
				t.Errorf("tagCommits = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestPinSource(t *testing.T) {
	const commit = "0123456789abcdef0123456789abcdef01234567"
	for _, status := range []int{http.StatusOK, http.StatusNotFound, http.StatusForbidden, http.StatusServiceUnavailable} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				if got, want := req.URL.String(), "https://github.com/NVIDIA/open-gpu-kernel-modules/archive/"+commit+".tar.gz"; got != want {
					t.Fatalf("archive URL = %q, want immutable commit URL %q", got, want)
				}
				return &http.Response{
					StatusCode: status,
					Status:     http.StatusText(status),
					Body:       io.NopCloser(strings.NewReader("archive")),
				}, nil
			})}
			pin, err := pinSource(context.Background(), client, "550.54.14", commit)
			if status != http.StatusOK {
				if err == nil {
					t.Fatalf("archive failure HTTP %d accepted: %+v", status, pin)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if pin.commit != commit || pin.sha256 != "0eb3e36bfb24dcd9bb1d1bece1531216b59539a8fde17ee80224af0653c92aa3" {
				t.Fatalf("unexpected archive pin: %+v", pin)
			}
		})
	}
}

func TestReplacePinsPreservesOtherDeclarations(t *testing.T) {
	block, _ := render([]sourcePin{{version: "550.54.14"}})
	original := "module(name = \"gvisor\")\n"
	first, err := replacePins(original, block)
	if err != nil {
		t.Fatal(err)
	}
	const suffix = "\nhttp_archive(name = \"unrelated\")\n"
	updated, err := replacePins(first+suffix, block)
	if err != nil {
		t.Fatal(err)
	}
	if updated != first+suffix {
		t.Fatalf("rewriting identical pins changed unrelated content:\n%s", updated)
	}
	for _, malformed := range []string{beginMarker, endMarker, endMarker + beginMarker, first + first} {
		if _, err := replacePins(malformed, block); err == nil {
			t.Errorf("accepted malformed generator delimiters %q", malformed)
		}
	}
}
