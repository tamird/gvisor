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
	_ "embed"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// Complete notices copied from the inputs that exposed classifier mistakes:
// bsd.txt and openssl.txt: https://github.com/apache/arrow/blob/e03105efc/go/LICENSE.txt
// llvm.txt: https://github.com/llvm/llvm-project/blob/85ac56026/LICENSE.TXT
var (
	//go:embed testdata/bsd.txt
	bsdText string
	//go:embed testdata/openssl.txt
	opensslText string
	//go:embed testdata/llvm.txt
	llvmText string
)

func TestParseGitRefs(t *testing.T) {
	const (
		first  = "1111111111111111111111111111111111111111"
		second = "2222222222222222222222222222222222222222"
		tag    = "refs/tags/v1"
		branch = "refs/heads/v1"
	)
	for _, test := range []struct {
		name, output, want string
		refs               []string
		wantErr            bool
	}{
		{
			name: "lightweight tag", refs: []string{tag, branch},
			output: first + "\t" + tag + "\n", want: first,
		},
		{
			name: "peeled tag", refs: []string{tag, branch},
			output: first + "\t" + tag + "\n" + second + "\t" + tag + "^{}\n", want: second,
		},
		{
			name: "branch", refs: []string{tag, branch},
			output: first + "\t" + branch + "\n", want: first,
		},
		{
			name: "same peeled tag and branch", refs: []string{tag, branch},
			output: first + "\t" + tag + "\n" + second + "\t" + tag + "^{}\n" + second + "\t" + branch + "\n", want: second,
		},
		{
			name: "conflicting tag and branch", refs: []string{tag, branch},
			output: first + "\t" + tag + "\n" + second + "\t" + branch + "\n",
		},
		{
			name: "qualified branch", refs: []string{branch},
			output: first + "\t" + tag + "\n" + second + "\t" + branch + "\n", want: second,
		},
		{
			name: "ref tail is not exact", refs: []string{tag, branch},
			output: first + "\trefs/tags/nested/" + tag + "\n",
		},
		{
			name: "missing", refs: []string{tag, branch},
		},
		{
			name: "invalid object ID", refs: []string{tag},
			output: "1234\t" + tag + "\n", wantErr: true,
		},
		{
			name: "missing separator", refs: []string{tag},
			output: first + " " + tag + "\n", wantErr: true,
		},
		{
			name: "duplicate ref", refs: []string{tag},
			output: first + "\t" + tag + "\n" + second + "\t" + tag + "\n", wantErr: true,
		},
		{
			name: "missing tag record", refs: []string{tag},
			output: first + "\t" + tag + "^{}\n", wantErr: true,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := parseGitRefs(test.output, test.refs)
			if (err != nil) != test.wantErr || got != test.want {
				t.Errorf("parseGitRefs = (%q, %v), want (%q, error=%t)", got, err, test.want, test.wantErr)
			}
		})
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

func TestFetchGitHubUsesResolvedCommit(t *testing.T) {
	// An archive URL can contain an abbreviated hash. Fetching its license
	// must use the full ID returned by the API, matching the recorded source.
	const commit = "1234567890abcdef1234567890abcdef12345678"
	const ref = "1234567890ab"
	const license = `MIT License

Copyright (c) 2026 Example Authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
`
	apiURL := "https://api.github.com/repos/example/project/commits/" + ref
	licenseURL := "https://raw.githubusercontent.com/example/project/" + commit + "/LICENSE"
	oldClient := httpClient
	t.Cleanup(func() { httpClient = oldClient })
	t.Setenv("GITHUB_TOKEN", "")
	var requests []string
	httpClient = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		url := r.URL.String()
		requests = append(requests, url)
		var body string
		switch url {
		case apiURL:
			body = commit
		case licenseURL:
			body = license
		default:
			return nil, fmt.Errorf("unexpected license request %s", url)
		}
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(body))}, nil
	})}
	got, err := fetchGitHub("https://github.com/example/project/archive/" + ref + ".tar.gz")
	if err != nil {
		t.Fatal(err)
	}
	if got.commit != commit || !slices.Equal(got.license, Licenses{"MIT"}) {
		t.Errorf("fetchGitHub = %+v, want commit %s and MIT", got, commit)
	}
	if want := []string{apiURL, licenseURL}; !slices.Equal(requests, want) {
		t.Errorf("requests = %v, want resolution then immutable license fetch %v", requests, want)
	}
}

func TestResolveGitHubCommitFullHashNeedsNoHTTP(t *testing.T) {
	const commit = "1234567890abcdef1234567890abcdef12345678"
	oldClient := httpClient
	t.Cleanup(func() { httpClient = oldClient })
	httpClient = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		err := fmt.Errorf("resolving a full hash must not request %s", r.URL)
		t.Error(err)
		return nil, err
	})}
	got, err := resolveGitHubCommit("example", "project", commit)
	if err != nil || got != commit {
		t.Errorf("resolveGitHubCommit = (%q, %v), want (%q, nil)", got, err, commit)
	}
}

func TestClassify(t *testing.T) {
	apache, _, ok := strings.Cut(llvmText, "---- LLVM Exceptions to the Apache 2.0 License ----")
	if !ok {
		t.Fatal("LLVM fixture has no exception boundary")
	}
	for _, test := range []struct {
		name, text string
		want       Licenses
	}{
		{"separate grants", opensslText + bsdText + opensslText, Licenses{"BSD-3-Clause", "OpenSSL"}},
		{"llvm", llvmText, Licenses{"Apache-2.0 WITH LLVM-exception", "NCSA"}},
		{"apache without exception", apache, Licenses{"Apache-2.0"}},
		{"unknown", "All rights reserved. Do not redistribute.", nil},
		{"reference", "See https://opensource.org/licenses/MIT for background.", nil},
		{"title", "GNU General Public License Version 2, June 1991", nil},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := classify(test.text)
			if (err != nil) != (test.want == nil) {
				t.Fatalf("classify error = %v, want licenses %v", err, test.want)
			}
			if !slices.Equal(got, test.want) {
				t.Errorf("classify = %v, want %v", got, test.want)
			}
		})
	}
}

func TestVerifyProblems(t *testing.T) {
	deps := []dep{
		{name: "example.com/mod", kind: kindGoModule, version: "v1.2.0", modulePath: "example.com/mod"},
		{name: "some-archive", kind: kindArchive, url: "https://github.com/a/b/archive/refs/tags/v3.tar.gz", sha256: "cafe"},
	}
	entries := []Entry{
		{Dependency: "example.com/mod", Version: "v1.2.0", Retrieved: "2026-08-26", Commit: "abc", SHA256: "beef", License: Licenses{"MIT"}},
		{Dependency: "some-archive", Version: "https://github.com/a/b/archive/refs/tags/v3.tar.gz", Retrieved: "2026-08-26", Commit: "def", SHA256: "cafe", License: Licenses{"Apache-2.0"}},
	}
	if problems := verifyProblems(deps, entries); len(problems) != 0 {
		t.Errorf("verifyProblems on up-to-date entries = %v, want none", problems)
	}
	for _, id := range []License{"NOASSERTION", "Apache-2.0 WITH LLVM-exception", "GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later"} {
		entries[0].License = Licenses{id}
		if problems := verifyProblems(deps, entries); len(problems) != 0 {
			t.Errorf("verifyProblems with explicit %s = %v", id, problems)
		}
	}
	entries[0].License = Licenses{"MIT"}

	// A version bump without re-fetching must be flagged, for both kinds.
	deps[0].version = "v1.3.0"
	deps[1].url = "https://github.com/a/b/archive/refs/tags/v4.tar.gz"
	problems := verifyProblems(deps, entries)
	if len(problems) != 2 ||
		!strings.Contains(problems[0], `example.com/mod was audited at "v1.2.0", but is now "v1.3.0"`) ||
		!strings.Contains(problems[1], "some-archive was audited at") {
		t.Errorf("verifyProblems after version bump = %v, want two audited-at problems", problems)
	}
	// A changed archive pin (same URL) must also be flagged.
	deps[0].version = "v1.2.0"
	deps[1].url = "https://github.com/a/b/archive/refs/tags/v3.tar.gz"
	deps[1].sha256 = "d00d"
	problems = verifyProblems(deps, entries)
	if len(problems) != 1 || !strings.Contains(problems[0], `some-archive was audited with sha256 "cafe", but is now pinned to "d00d"`) {
		t.Errorf("verifyProblems after pin change = %v, want one sha256 problem", problems)
	}
	// Missing and stale entries are still flagged.
	deps[0].version = "v1.2.0"
	deps[1].name = "renamed-archive"
	problems = verifyProblems(deps, entries)
	if len(problems) != 2 ||
		!strings.Contains(problems[0], "missing entry for renamed-archive") ||
		!strings.Contains(problems[1], "stale entry for some-archive") {
		t.Errorf("verifyProblems after rename = %v, want missing+stale", problems)
	}
}

// repoRoot finds the directory containing both YAML files: the checkout root
// under `go test`, or the runfiles root under Bazel.
func repoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	// First check upwards (works for `go test` and Bazel).
	curr := dir
	for range 8 {
		if _, err := os.Stat(filepath.Join(curr, "governance/licensing.yaml")); err == nil {
			return curr
		}
		curr = filepath.Dir(curr)
	}
	// In some test environments, the repo is in a subdirectory of the runfiles root.
	var found string
	err = filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			if rel, err := filepath.Rel(dir, path); err == nil && strings.Count(rel, string(filepath.Separator)) > 4 {
				return fs.SkipDir
			}
			return nil
		}
		if strings.HasSuffix(filepath.ToSlash(path), "governance/licensing.yaml") {
			candidate := filepath.Dir(filepath.Dir(path))
			if _, err := os.Stat(filepath.Join(candidate, "tools/licensecheck/dependencies.yaml")); err == nil {
				found = candidate
				return fs.SkipAll
			}
		}
		return nil
	})
	if err == nil && found != "" {
		return found
	}
	t.Fatal("cannot find governance/licensing.yaml above or below the working directory")
	return ""
}

// TestLicensePolicy verifies that every dependency in dependencies.yaml
// either uses only licenses in governance/licensing.yaml's allowed_licenses,
// or is on its exceptions list.
func TestLicensePolicy(t *testing.T) {
	root := repoRoot(t)
	entries, err := readEntries(filepath.Join(root, "tools/licensecheck/dependencies.yaml"))
	if err != nil {
		t.Fatalf("cannot read dependencies.yaml: %v", err)
	}
	policy, err := ReadPolicy(filepath.Join(root, "governance/licensing.yaml"))
	if err != nil {
		t.Fatalf("cannot read licensing.yaml: %v", err)
	}
	for _, problem := range CheckPolicy(entries, policy) {
		t.Error(problem)
	}
}

func TestCheckPolicy(t *testing.T) {
	entries := []Entry{
		{Dependency: "a", License: Licenses{"MIT"}},
		{Dependency: "b", License: Licenses{"MPL-2.0"}},
		{Dependency: "c", License: Licenses{"Apache-2.0", "MPL-2.0"}},
		{Dependency: "d", License: Licenses{"GPL-2.0-only"}},
	}
	policy := &Policy{
		AllowedLicenses: []License{"Apache-2.0", "MIT"},
		Exceptions: []Exception{
			{Dependency: "b", License: Licenses{"MPL-2.0"}, ExceptionRationale: "vendored"},
			{Dependency: "c", License: Licenses{"Apache-2.0", "MPL-2.0"}, ExceptionRationale: "notices"},
		},
	}
	if problems := CheckPolicy(entries, policy); len(problems) != 1 || !strings.Contains(problems[0], "d uses disallowed licenses") {
		t.Errorf("CheckPolicy = %v, want a single problem for d", problems)
	}
	// A license mismatch, a stale exception, an unnecessary exception, and
	// the now-uncovered c and d are all flagged.
	policy.Exceptions = []Exception{
		{Dependency: "a", License: Licenses{"MIT"}, ExceptionRationale: "redundant"},
		{Dependency: "b", License: Licenses{"Unlicense"}, ExceptionRationale: "wrong license"},
		{Dependency: "z", License: Licenses{"MPL-2.0"}, ExceptionRationale: "no longer a dependency"},
	}
	problems := CheckPolicy(entries, policy)
	for _, want := range []string{
		"unnecessary exception for a",
		"exception for b lists licenses Unlicense",
		"exception for z, which is not a dependency",
		"c uses disallowed licenses",
		"d uses disallowed licenses",
	} {
		if !slices.ContainsFunc(problems, func(p string) bool { return strings.Contains(p, want) }) {
			t.Errorf("CheckPolicy = %v, missing problem %q", problems, want)
		}
	}
	if len(problems) != 5 {
		t.Errorf("CheckPolicy returned %d problems, want 5: %v", len(problems), problems)
	}
}
