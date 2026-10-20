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
	"bytes"
	"crypto/sha256"
	_ "embed"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"

	"gvisor.dev/gvisor/pkg/zip"
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

func TestAuditedExtensionRepos(t *testing.T) {
	// Bazel 8.8.1 JsonOutputFormatter uses ModuleExtensionId.toString() for
	// these keys. The isolated apt key matches container_apt's lock record.
	graph, err := parseModGraph(`{
  "extensionUsages": [
    {"key": "@@rules_distroless+//apt:extensions.bzl%apt", "used_repos": ["release_tools"]},
    {"key": "@@rules_distroless+//apt:extensions.bzl%apt%<root>+container_apt", "used_repos": ["container_test_tools"]},
    {"key": "@@gazelle+//:extensions.bzl%go_deps", "used_repos": ["go_dependency"]},
    {"key": "@@rules_python+//python/extensions:pip.bzl%pip", "used_repos": ["python_packages"]},
    {"key": "@@//:MODULE.bazel%_repo_rules http_archive", "used_repos": ["archive"]},
    {"key": "@@//:MODULE.bazel%_repo_rules http_file", "used_repos": ["file"]},
    {"key": "@@//:MODULE.bazel%_repo_rules go_repository", "used_repos": ["go_archive"]},
    {"key": "@@//:MODULE.bazel%_repo_rules module_proxy", "used_repos": ["proxy"]},
    {"key": "@@rules_go+//go:extensions.bzl%go_sdk", "used_repos": ["go_sdk"]},
    {"key": "@@rules_go+//go:extensions.bzl%go_sdk%<root>+apt", "used_repos": ["isolated_sdk"]}
  ]
}`)
	if err != nil {
		t.Fatal(err)
	}
	got := graph.auditedRepos()
	want := []string{"release_tools", "container_test_tools", "go_dependency", "python_packages", "archive", "file", "go_archive", "proxy"}
	if !slices.Equal(got, want) {
		t.Fatalf("audited repositories = %v, want %v", got, want)
	}
}

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

// Verbatim notices from the modules that exposed incomplete discovery:
// https://github.com/JohnCGriffin/overflow/blob/46fa312c3/README.md
// https://github.com/aymanbagabas/go-udiff/tree/4608934d2
var (
	//go:embed testdata/overflow-readme.txt
	overflowReadme string
	//go:embed testdata/udiff-bsd.txt
	udiffBSD string
	//go:embed testdata/udiff-mit.txt
	udiffMIT string
)

func TestParseGitHubCodeloadURL(t *testing.T) {
	for _, test := range []struct {
		url  string
		want [3]string
	}{
		{
			url:  "https://codeload.github.com/torvalds/linux/tar.gz/fd73f4a6659897191fa0d40695fe370925dd3780",
			want: [3]string{"torvalds", "linux", "fd73f4a6659897191fa0d40695fe370925dd3780"},
		},
		{
			url:  "https://codeload.github.com/example/project/tar.gz/refs/heads/release/v1",
			want: [3]string{"example", "project", "refs/heads/release/v1"},
		},
		{
			url:  "https://codeload.github.com/example/project/zip/refs/tags/v1.0",
			want: [3]string{"example", "project", "refs/tags/v1.0"},
		},
	} {
		t.Run(test.url, func(t *testing.T) {
			owner, repo, ref, err := parseGitHubURL(test.url)
			if err != nil {
				t.Fatal(err)
			}
			if got := [3]string{owner, repo, ref}; got != test.want {
				t.Errorf("parseGitHubURL = %q, want %q", got, test.want)
			}
		})
	}
}

func TestFetchPreservesManualArtifact(t *testing.T) {
	hash := fmt.Sprintf("%x", sha256.Sum256([]byte("release artifact")))
	for _, audit := range []struct {
		name     string
		licenses Licenses
		manual   bool
	}{
		{"local reference", Licenses{"LicenseRef-CodeQL", "MIT"}, false},
		{"standard terms", Licenses{"Apache-2.0", "BSD-3-Clause"}, true},
	} {
		t.Run(audit.name, func(t *testing.T) {
			p := Paths{YAML: filepath.Join(t.TempDir(), "dependencies.yaml")}
			original := Entry{
				Dependency: "bundle", Version: "https://example.com/bundle-v1.tar.gz",
				Retrieved: "2026-10-02", SHA256: hash,
				License: audit.licenses, ManuallyAudited: audit.manual,
			}
			automatic := original
			automatic.Dependency = "automatic"
			automatic.License = Licenses{"BSD-3-Clause"}
			automatic.ManuallyAudited = false
			if err := writeEntries(p.YAML, []Entry{original, automatic}); err != nil {
				t.Fatal(err)
			}
			deps := []dep{
				{name: original.Dependency, kind: kindArchive, url: original.Version, sha256: hash},
				{name: automatic.Dependency, kind: kindArchive, url: automatic.Version, sha256: hash},
			}
			// Successful discovery of the repository's MIT notice must not
			// overwrite the artifact's complete manually audited terms.
			retrieve := func(d dep, _ Entry) (*fetched, error) {
				licenses, err := classify(udiffMIT)
				return &fetched{sha256: d.sha256, license: licenses}, err
			}
			if err := fetch(p, deps, retrieve); err != nil {
				t.Fatal(err)
			}
			entries, err := readEntries(p.YAML)
			if err != nil {
				t.Fatal(err)
			}
			if got, want := len(entries), 2; got != want {
				t.Fatalf("catalog has %d entries, want %d", got, want)
			}
			if got, want := entries[0], original; !reflect.DeepEqual(got, want) {
				t.Errorf("manual entry = %+v, want unchanged %+v", got, want)
			}
			if got, want := entries[1].License, (Licenses{"MIT"}); !slices.Equal(got, want) {
				t.Errorf("automatic licenses = %v, want %v", got, want)
			}
			// Recognizing or retaining terms does not approve their use.
			policy := &Policy{AllowedLicenses: []License{"MIT"}}
			want := []string{fmt.Sprintf("bundle uses disallowed licenses %v and has no exception in the policy", original.License)}
			if got := CheckPolicy(entries, policy); !slices.Equal(got, want) {
				t.Errorf("CheckPolicy = %v, want %v", got, want)
			}
			policy.Exceptions = []Exception{{Dependency: "bundle", License: original.License, ExceptionRationale: "reviewed artifact terms"}}
			if got, want := CheckPolicy(entries, policy), []string(nil); !slices.Equal(got, want) {
				t.Errorf("CheckPolicy with scoped exception = %v, want %v", got, want)
			}
		})
	}
}

func TestFetchRejectsInvalidManualAudit(t *testing.T) {
	hash := fmt.Sprintf("%x", sha256.Sum256([]byte("release artifact v1")))
	changedHash := fmt.Sprintf("%x", sha256.Sum256([]byte("release artifact v2")))
	const url = "https://example.com/bundle-v1.tar.gz"
	for _, test := range []struct {
		name, url, hash string
		licenses        Licenses
		manual          bool
		source          string
		duplicate       bool
		wantErr         string
	}{
		{
			name: "changed URL", url: "https://example.com/bundle-v2.tar.gz", hash: hash,
			licenses: Licenses{"LicenseRef-CodeQL"},
			wantErr:  `re-audit the license of bundle: bundle was audited at "` + url + `", but is now "https://example.com/bundle-v2.tar.gz"`,
		},
		{
			name: "changed bytes", url: url, hash: changedHash,
			licenses: Licenses{"LicenseRef-CodeQL"},
			wantErr:  fmt.Sprintf("re-audit the license of bundle: bundle was audited with sha256 %q, but is now pinned to %q", hash, changedHash),
		},
		{
			name: "removed pin", url: url,
			licenses: Licenses{"Apache-2.0", "BSD-3-Clause"}, manual: true,
			wantErr: "re-audit the license of bundle: bundle has manually audited terms without an artifact checksum pin",
		},
		{
			name: "empty local identifier", url: url, hash: hash,
			licenses: Licenses{"LicenseRef-"},
			wantErr:  `re-audit the license of bundle: bundle has unknown license "LicenseRef-"`,
		},
		{
			name: "invalid local identifier", url: url, hash: hash,
			licenses: Licenses{"LicenseRef-Code_QL"},
			wantErr:  `re-audit the license of bundle: bundle has unknown license "LicenseRef-Code_QL"`,
		},
		{
			name: "duplicate entry", url: url, hash: hash,
			licenses:  Licenses{"LicenseRef-CodeQL"},
			duplicate: true,
			wantErr:   "duplicate entry for bundle in %s",
		},
		{
			name: "manual notice commit", url: url, hash: hash, manual: true,
			licenses: Licenses{"MIT"},
			source:   "https://raw.githubusercontent.com/example/upstream/0123456789abcdef0123456789abcdef01234567/LICENSE",
			wantErr:  "re-audit the license of bundle: bundle: license_source commit does not match the audited commit",
		},
		{
			name: "mutable manual notice", url: url, hash: hash,
			licenses: Licenses{"LicenseRef-Tool"},
			source:   "https://raw.githubusercontent.com/example/upstream/main/LICENSE",
			wantErr:  "re-audit the license of bundle: bundle: license_source must identify a GitHub notice pinned to a full commit",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			p := Paths{YAML: filepath.Join(t.TempDir(), "dependencies.yaml")}
			original := Entry{
				Dependency: "bundle", Version: url, Retrieved: "2026-10-02",
				SHA256: hash, License: test.licenses, ManuallyAudited: test.manual,
				LicenseSource: test.source,
			}
			entries := []Entry{original}
			wantErr := test.wantErr
			if test.duplicate {
				entries = append(entries, original)
				wantErr = fmt.Sprintf(wantErr, p.YAML)
			}
			if err := writeEntries(p.YAML, entries); err != nil {
				t.Fatal(err)
			}
			before, err := os.ReadFile(p.YAML)
			if err != nil {
				t.Fatal(err)
			}
			deps := []dep{{name: "bundle", kind: kindArchive, url: test.url, sha256: test.hash}}
			err = fetch(p, deps, func(d dep, _ Entry) (*fetched, error) {
				t.Error("retrieved a notice before rejecting the invalid manual audit")
				return nil, fmt.Errorf("unexpected notice retrieval for %s", d.name)
			})
			if err == nil || err.Error() != wantErr {
				t.Fatalf("fetch = %v, want %q", err, wantErr)
			}
			after, err := os.ReadFile(p.YAML)
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(before, after) {
				t.Error("fetch rewrote the invalid catalog")
			}
		})
	}
}

func TestLicenseSourceIdentity(t *testing.T) {
	const artifact = "https://github.com/example/builder/releases/download/v1/tool.tar.gz"
	const commit = "0123456789abcdef0123456789abcdef01234567"
	const source = "https://raw.githubusercontent.com/example/upstream/" + commit + "/COPYING"
	hash := fmt.Sprintf("%x", sha256.Sum256([]byte("release artifact")))
	changedHash := fmt.Sprintf("%x", sha256.Sum256([]byte("changed artifact")))
	for _, test := range []struct {
		name, url, hash, source string
		kind                    depKind
		wantCommit, wantErr     string
	}{
		{name: "pinned", kind: kindArchive, url: artifact, hash: hash, source: source, wantCommit: commit},
		{name: "automatic", kind: kindArchive, url: artifact, hash: hash},
		{
			name: "changed URL", kind: kindArchive, url: artifact + "?changed", hash: hash, source: source,
			wantErr: "license_source artifact identity changed or is unpinned; audit the new artifact and refresh its notice source",
		},
		{
			name: "changed digest", kind: kindArchive, url: artifact, hash: changedHash, source: source,
			wantErr: "license_source artifact identity changed or is unpinned; audit the new artifact and refresh its notice source",
		},
		{
			name: "unpinned artifact", kind: kindArchive, url: artifact, source: source,
			wantErr: "license_source artifact identity changed or is unpinned; audit the new artifact and refresh its notice source",
		},
		{
			name: "Go module", kind: kindGoModule, url: artifact, hash: hash, source: source,
			wantErr: "license_source artifact identity changed or is unpinned; audit the new artifact and refresh its notice source",
		},
		{
			name: "mutable notice", kind: kindArchive, url: artifact, hash: hash, source: strings.ReplaceAll(source, commit, "main"),
			wantErr: "license_source must identify a GitHub notice pinned to a full commit",
		},
		{
			name: "HTML notice", kind: kindArchive, url: artifact, hash: hash, source: "https://github.com/example/upstream/blob/" + commit + "/COPYING",
			wantErr: "license_source must identify a GitHub notice pinned to a full commit",
		},
		{
			name: "notice traversal", kind: kindArchive, url: artifact, hash: hash, source: strings.ReplaceAll(source, "/COPYING", "/../main/COPYING"),
			wantErr: "license_source must name a recognized raw GitHub root notice",
		},
		{
			name: "encoded traversal", kind: kindArchive, url: artifact, hash: hash, source: strings.ReplaceAll(source, "/COPYING", "/%2e%2e/main/COPYING"),
			wantErr: "license_source must name a recognized raw GitHub root notice",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			d := dep{name: "tool", kind: test.kind, url: test.url, sha256: test.hash}
			e := Entry{Version: artifact, SHA256: hash, LicenseSource: test.source}
			got, err := licenseSourceCommit(d, e)
			if test.wantErr != "" {
				if err == nil || err.Error() != test.wantErr {
					t.Fatalf("licenseSourceCommit = %q, %v; want error %q", got, err, test.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if want := test.wantCommit; got != want {
				t.Errorf("licenseSourceCommit = %q, want %q", got, want)
			}
		})
	}
}

func TestFetchLicenseSource(t *testing.T) {
	const commit = "0123456789abcdef0123456789abcdef01234567"
	const source = "https://raw.githubusercontent.com/example/upstream/" + commit + "/COPYING"
	hash := fmt.Sprintf("%x", sha256.Sum256([]byte("release artifact")))
	for _, test := range []struct {
		name        string
		manual      bool
		licenses    Licenses
		retrieveErr error
		wantErr     string
	}{
		{name: "automatic", licenses: Licenses{"BSD-3-Clause"}},
		{
			name: "unavailable notice", licenses: Licenses{"BSD-3-Clause"},
			retrieveErr: errors.New("notice unavailable"),
			wantErr:     "cannot fetch licenses for tool; fix the reported source or notice errors",
		},
		{name: "manual standard terms", manual: true, licenses: Licenses{"Apache-2.0", "BSD-3-Clause"}},
		{name: "manual local reference", licenses: Licenses{"LicenseRef-Tool", "MIT"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			p := Paths{YAML: filepath.Join(t.TempDir(), "dependencies.yaml")}
			d := dep{name: "tool", kind: kindArchive, url: "https://example.com/tool.tar.gz", sha256: hash}
			original := Entry{
				Dependency: d.name, Version: d.url, SHA256: hash,
				Retrieved: "2026-10-02", Commit: commit, License: test.licenses,
				ManuallyAudited: test.manual, LicenseSource: source,
			}
			if err := writeEntries(p.YAML, []Entry{original}); err != nil {
				t.Fatal(err)
			}
			err := fetch(p, []dep{d}, func(d dep, _ Entry) (*fetched, error) {
				if test.retrieveErr != nil {
					return nil, test.retrieveErr
				}
				licenses, err := classify(udiffMIT)
				return &fetched{commit: commit, sha256: d.sha256, license: licenses}, err
			})
			if test.wantErr != "" {
				if err == nil || err.Error() != test.wantErr {
					t.Fatalf("fetch = %v, want %q", err, test.wantErr)
				}
			} else if err != nil {
				t.Fatal(err)
			}
			entries, err := readEntries(p.YAML)
			if err != nil {
				t.Fatal(err)
			}
			if got, want := len(entries), 1; got != want {
				t.Fatalf("catalog has %d entries, want %d", got, want)
			}
			if test.wantErr != "" || original.manuallyAudited() {
				if got, want := entries[0], original; !reflect.DeepEqual(got, want) {
					t.Errorf("retained entry = %+v, want unchanged %+v", got, want)
				}
			} else {
				if got, want := entries[0].License, (Licenses{"MIT"}); !slices.Equal(got, want) {
					t.Errorf("automatic licenses = %v, want %v", got, want)
				}
				if got, want := entries[0].LicenseSource, source; got != want {
					t.Errorf("notice source = %q, want %q", got, want)
				}
			}
			if got, want := verifyProblems([]dep{d}, entries), []string(nil); !slices.Equal(got, want) {
				t.Errorf("verifyProblems = %v, want %v", got, want)
			}
			if original.manuallyAudited() || test.wantErr != "" {
				return
			}
			entries[0].Commit = strings.Repeat("a", 40)
			want := []string{"tool: license_source commit does not match the audited commit"}
			if got := verifyProblems([]dep{d}, entries); !slices.Equal(got, want) {
				t.Errorf("verifyProblems with mismatched commit = %v, want %v", got, want)
			}
		})
	}
}

func TestModuleLicenses(t *testing.T) {
	const prefix = "example.com/module@v1.0.0/"
	for _, test := range []struct {
		name    string
		files   map[string]string
		want    Licenses
		wantErr string
	}{
		{
			name: "split notices",
			files: map[string]string{
				prefix + "LICENSE-BSD": udiffBSD,
				prefix + "LICENSE-MIT": udiffMIT,
			},
			want: Licenses{"BSD-3-Clause", "MIT"},
		},
		{
			name: "source files alongside notices",
			files: map[string]string{
				prefix + "LICENSE":         udiffMIT,
				prefix + "LICENSE.docs":    udiffBSD,
				prefix + "license.go":      "package licensecheck\n",
				prefix + "license_test.go": "package licensecheck\n",
			},
			want: Licenses{"BSD-3-Clause", "MIT"},
		},
		{
			name: "unrecognized notice alongside known notice",
			files: map[string]string{
				prefix + "LICENSE":     "All rights reserved. Do not redistribute.",
				prefix + "LICENSE-MIT": udiffMIT,
			},
			wantErr: "LICENSE: cannot classify license text",
		},
		{
			name:  "readme grant",
			files: map[string]string{prefix + "README.md": overflowReadme},
			want:  Licenses{"MIT"},
		},
		{
			name: "primary notices take precedence",
			files: map[string]string{
				prefix + "License_BSD": udiffBSD,
				prefix + "README.md":   overflowReadme,
			},
			want: Licenses{"BSD-3-Clause"},
		},
		{
			name: "unrecognized primary notice",
			files: map[string]string{
				prefix + "COPYING":   "All rights reserved. Do not redistribute.",
				prefix + "README.md": overflowReadme,
			},
			wantErr: "cannot classify license text",
		},
		{
			name:    "readme reference is not a grant",
			files:   map[string]string{prefix + "README": "See https://opensource.org/licenses/MIT for background."},
			wantErr: "cannot classify license text",
		},
		{
			name: "root membership",
			files: map[string]string{
				prefix + "source.go":               udiffMIT,
				prefix + "nested/LICENSE":          udiffMIT,
				"example.com/other@v1.0.0/LICENSE": udiffMIT,
			},
			wantErr: "no license notice at module root",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			var body bytes.Buffer
			z := zip.NewWriter(&body)
			for name, text := range test.files {
				w, err := z.Create(name)
				if err != nil {
					t.Fatal(err)
				}
				if _, err := w.Write([]byte(text)); err != nil {
					t.Fatal(err)
				}
			}
			if err := z.Close(); err != nil {
				t.Fatal(err)
			}
			got, err := moduleLicenses(body.Bytes(), prefix)
			if test.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), test.wantErr) {
					t.Fatalf("moduleLicenses = %v, %v, want error %q", got, err, test.wantErr)
				}
				return
			}
			if err != nil || !slices.Equal(got, test.want) {
				t.Fatalf("moduleLicenses = %v, %v, want %v", got, err, test.want)
			}
		})
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
	// The same declared identity can retain reviewed metadata after a fetch
	// failure. A Go entry's computed ZIP hash is not an enumerated archive pin.
	for i, d := range deps {
		if !d.canRetain(entries[i]) {
			t.Errorf("cannot retain current entry for %s", d.name)
		}
		empty := entries[i]
		empty.License = nil
		if d.canRetain(empty) {
			t.Errorf("retained entry without licenses for %s", d.name)
		}
	}
	for _, id := range []License{"NOASSERTION", "Apache-2.0 WITH LLVM-exception", "GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later"} {
		entries[0].License = Licenses{id}
		if problems := verifyProblems(deps, entries); len(problems) != 0 {
			t.Errorf("verifyProblems with explicit %s = %v", id, problems)
		}
	}
	entries[0].License = Licenses{"MIT"}
	for _, id := range []License{"LicenseRef-CodeQL", "LicenseRef-", "LicenseRef-Code_QL"} {
		entries[1].License = Licenses{id}
		problems := verifyProblems(deps, entries)
		if (len(problems) == 0) != (id == "LicenseRef-CodeQL") {
			t.Errorf("verifyProblems with explicit %s = %v", id, problems)
		}
	}
	entries[1].License = Licenses{"Apache-2.0"}

	// A version bump without re-fetching must be flagged, for both kinds.
	deps[0].version = "v1.3.0"
	deps[1].url = "https://github.com/a/b/archive/refs/tags/v4.tar.gz"
	for i, d := range deps {
		if d.canRetain(entries[i]) {
			t.Errorf("retained stale source for %s", d.name)
		}
	}
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
	if deps[1].canRetain(entries[1]) {
		t.Error("retained entry after archive pin changed")
	}
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

func TestQemuUserRepoRefs(t *testing.T) {
	// A public hub maps indirect repos that the root module cannot import by
	// apparent name. Firmware and system tools are outside this user-mode audit.
	mapping := map[string]string{
		"qemu_user_prebuilt_linux_amd64_aarch64": "rules_qemu++qemu+arm",
		"qemu_user_prebuilt_linux_arm64_x86_64":  "rules_qemu++qemu+x86",
		"qemu_user_toolchains":                   "rules_qemu++qemu+hub",
		"qemu_system_data_prebuilt_linux_amd64":  "rules_qemu++qemu+firmware",
		"qemu_img_prebuilt_linux_amd64":          "rules_qemu++qemu+img",
	}
	got, err := qemuUserRepoRefs(mapping)
	if err != nil {
		t.Fatal(err)
	}
	want := []repoRef{
		{"@@rules_qemu++qemu+arm", "qemu_user_prebuilt_linux_amd64_aarch64"},
		{"@@rules_qemu++qemu+x86", "qemu_user_prebuilt_linux_arm64_x86_64"},
	}
	if !slices.Equal(got, want) {
		t.Errorf("qemuUserRepoRefs() = %v, want %v", got, want)
	}
	for _, invalid := range []map[string]string{
		{},
		{"qemu_user_toolchains": "rules_qemu++qemu+hub"},
		{"qemu_user_prebuilt_linux_amd64_aarch64": ""},
	} {
		if _, err := qemuUserRepoRefs(invalid); err == nil {
			t.Errorf("qemuUserRepoRefs(%v) succeeded, want missing archive identity error", invalid)
		}
	}
}
