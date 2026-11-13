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
	"net/http"
	"strings"
	"testing"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
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
