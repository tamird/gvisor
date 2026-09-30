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

package licensecheck

import (
	"slices"
	"strings"
	"testing"
)

func TestModuleProxyScopes(t *testing.T) {
	const main = `{"Path":"example.com/main","Main":true}`
	first, err := moduleProxyDependencies("first", strings.NewReader(main+`
{"Path":"example.com/shared","Version":"v1.0.0"}
{"Path":"example.com/replaced","Version":"v1.0.0","Replace":{"Path":"example.com/fork","Version":"v1.2.0","Dir":"/resolver/cache/fork"}}
`))
	if err != nil {
		t.Fatal(err)
	}
	second, err := moduleProxyDependencies("second", strings.NewReader(main+`
{"Path":"example.com/shared","Version":"v1.1.0"}
`))
	if err != nil {
		t.Fatal(err)
	}
	got := append(first, second...)
	want := []dep{
		{name: "module_proxy/first/example.com/shared", kind: kindGoModule, modulePath: "example.com/shared", version: "v1.0.0"},
		{name: "module_proxy/first/example.com/replaced", kind: kindGoModule, modulePath: "example.com/fork", version: "v1.2.0"},
		{name: "module_proxy/second/example.com/shared", kind: kindGoModule, modulePath: "example.com/shared", version: "v1.1.0"},
	}
	if !slices.Equal(got, want) {
		t.Fatalf("proxy dependencies = %+v, want %+v", got, want)
	}
	entries := []Entry{
		{Dependency: want[0].name, Version: "example.com/shared@v1.0.0", Retrieved: "2026-09-30", License: Licenses{mit}},
		{Dependency: want[1].name, Version: "example.com/fork@v1.2.0", Retrieved: "2026-09-30", License: Licenses{mit}},
		{Dependency: want[2].name, Version: "example.com/shared@v1.1.0", Retrieved: "2026-09-30", License: Licenses{mit}},
	}
	if problems := verifyProblems(got, entries); len(problems) != 0 {
		t.Fatalf("complete scoped inventory: %v", problems)
	}
	// Removing one resolver scope must not hide the other scope's version.
	if problems := verifyProblems(got, entries[:2]); len(problems) != 1 || !strings.Contains(problems[0], "missing entry for "+want[2].name) {
		t.Errorf("missing scoped entry: %v", problems)
	}
	if problems := verifyProblems(got[:2], entries); len(problems) != 1 || !strings.Contains(problems[0], "stale entry for "+want[2].name) {
		t.Errorf("stale scoped entry: %v", problems)
	}
	// A replacement path change at the same version changes the audited source.
	got[1].modulePath = "example.com/another-fork"
	if problems := verifyProblems(got, entries); len(problems) != 1 || !strings.Contains(problems[0], "example.com/another-fork@v1.2.0") {
		t.Errorf("changed replacement: %v", problems)
	}
}

func TestModuleProxyInvalidInventory(t *testing.T) {
	const main = `{"Path":"example.com/main","Main":true}`
	for _, test := range []struct{ name, input, problem string }{
		{"empty", "", "0 main modules"},
		{"truncated", main + `{`, "invalid inventory"},
		{"missing path", main + `{}`, "missing or duplicate module path"},
		{"duplicate", main + main, "duplicate module path"},
		{"resolver error", main + `{"Path":"example.com/mod","Error":{"Err":"unavailable"}}`, "unavailable"},
		{"replacement error", main + `{"Path":"example.com/mod","Version":"v1.0.0","Replace":{"Error":{"Err":"unavailable"}}}`, "unavailable"},
		{"local replacement", main + `{"Path":"example.com/mod","Version":"v1.0.0","Replace":{"Path":"../local"}}`, "local replacement"},
		{"invalid version", main + `{"Path":"example.com/mod","Version":"latest"}`, "invalid selected module"},
	} {
		t.Run(test.name, func(t *testing.T) {
			if _, err := moduleProxyDependencies("scope", strings.NewReader(test.input)); err == nil || !strings.Contains(err.Error(), test.problem) {
				t.Errorf("inventory error = %v, want %q", err, test.problem)
			}
		})
	}
}
