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

package main

import (
	"strings"
	"testing"
)

func TestParseBoundaries(t *testing.T) {
	const header = "pkg: example\ngoos: linux\ngoarch: amd64\n"
	const noPackage = "goos: linux\ngoarch: amd64\n"
	valid := strings.Repeat("BenchmarkTiming-4 1 3 ns/op\n", 24)
	for _, tc := range []struct {
		name string
		input string
		wantError bool
	}{
		{"valid", header + valid, false},
		{"valid-no-package", noPackage + valid, false},
		{"mixed-package-presence", noPackage + valid + "pkg: example\n" + valid, true},
		{"mixed-package-absence", header + valid + "pkg:\n" + valid, true},
		{"zero", header + valid + "BenchmarkTiming-4 1 0 ns/op\n", true},
		{"infinity", header + valid + "BenchmarkTiming-4 1 +Inf ns/op\n", true},
		{"duplicate", header + valid + "BenchmarkTiming-4 1 3 ns/op 0.000000003 sec/op\n", true},
		{"mixed-package", header + valid + "pkg: other\n" + valid, true},
		{"mixed-os", header + valid + "goos: other\n" + valid, true},
		{"mixed-arch", header + valid + "goarch: arm64\n" + valid, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rows, err := parse("boundary", strings.NewReader(tc.input))
			if (err != nil) != tc.wantError {
				t.Fatalf("parse error = %v, want error %t", err, tc.wantError)
			}
			if err == nil && (len(rows) != 1 || len(rows[0].Values) != 24) {
				t.Fatalf("valid independent rows changed: %v", rows)
			}
		})
	}
}
