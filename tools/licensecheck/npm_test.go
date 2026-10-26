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
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
)

func TestNodeNpmDeclarations(t *testing.T) {
	t.Chdir(t.TempDir())
	const image = "images/runtimes/nodejs22.2.0"
	if err := os.MkdirAll(image, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(image+"/Dockerfile.x86_64", []byte("FROM ubuntu:jammy\n"), 0644); err != nil {
		t.Fatal(err)
	}
	// An existing image without the new source declaration must not make the
	// audit require an absent manifest or download an unpinned source.
	deps, err := enumerateNodeNpm()
	if err != nil {
		t.Fatal(err)
	}
	if got, want := len(deps), 0; got != want {
		t.Errorf("undeclared npm dependencies = %d, want %d", got, want)
	}
	if err := os.WriteFile(image+"/source.sha256", []byte("invalid\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := enumerateNodeNpm(); err == nil || err.Error() != "nodejs22.2.0: expected one SHA256 row for node-<full Git commit>.tar.gz" {
		t.Errorf("declared invalid source error = %v, want invalid manifest", err)
	}
}

func TestNodeArchiveSource(t *testing.T) {
	const commit = "26e72d85a611e222e3f06025de8a3c496923bacd"
	const hash = "75d385191001d110a6bfc9c057d9b2e6ba2f63134b0de0e1bb1f52917c2ff83f"
	const manifest = hash + "  node-" + commit + ".tar.gz\n"
	const invalid = "nodejs26.12.0: expected one SHA256 row for node-<full Git commit>.tar.gz"
	for _, test := range []struct {
		name, text, wantErr string
	}{
		{"declared archive", manifest, ""},
		{"missing row", "", invalid},
		{"multiple rows", manifest + manifest, invalid},
		{"mutable commit", strings.ReplaceAll(manifest, commit, "main"), invalid},
		{"invalid hash", strings.ReplaceAll(manifest, hash, "invalid"), invalid},
		{"traversing filename", strings.ReplaceAll(manifest, "node-", "../node-"), invalid},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := nodeArchiveSource("nodejs26.12.0", []byte(test.text))
			if test.wantErr != "" {
				if err == nil || err.Error() != test.wantErr {
					t.Fatalf("nodeArchiveSource() error = %v, want %q", err, test.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if want := (nodeSource{"nodejs26.12.0", commit, hash}); got != want {
				t.Errorf("nodeArchiveSource() = %v, want %v", got, want)
			}
		})
	}
}

type npmTarFile struct {
	name, body string
}

func npmTestArchive(t *testing.T, files ...npmTarFile) []byte {
	t.Helper()
	var body bytes.Buffer
	z := gzip.NewWriter(&body)
	writer := tar.NewWriter(z)
	for _, file := range files {
		if err := writer.WriteHeader(&tar.Header{Name: file.name, Mode: 0644, Size: int64(len(file.body))}); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write([]byte(file.body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	if err := z.Close(); err != nil {
		t.Fatal(err)
	}
	return body.Bytes()
}

func npmTestIntegrity(body []byte) string {
	sum := sha512.Sum512(body)
	return "sha512-" + base64.StdEncoding.EncodeToString(sum[:])
}

func TestNodeNpmLocks(t *testing.T) {
	const commit = "26e72d85a611e222e3f06025de8a3c496923bacd"
	prefix := "node-" + commit + "/tools/"
	lock := `{"lockfileVersion":3,"packages":{"":{},"node_modules/example":{"version":"1.0.0","resolved":"https://registry.npmjs.org/example/-/example-1.0.0.tgz","integrity":"` + npmTestIntegrity(nil) + `"}}}`
	files := []npmTarFile{{prefix + "doc/package-lock.json", lock}, {prefix + "eslint/package-lock.json", lock}}
	for _, test := range []struct {
		name    string
		files   []npmTarFile
		stale   bool
		wantErr string
	}{
		{"both canonical locks", files, false, ""},
		{"missing lock", files[:1], false, "nodejs26.12.0: missing tools/eslint/package-lock.json"},
		{"duplicate lock", append(slices.Clone(files), files[0]), false, prefix + "doc/package-lock.json: duplicate or nonregular lock"},
		{"overriding shrinkwrap", append(slices.Clone(files), npmTarFile{prefix + "doc/npm-shrinkwrap.json", lock}), false, prefix + "doc/npm-shrinkwrap.json overrides the audited package lock"},
		{"changed archive", files, true, ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			body := npmTestArchive(t, test.files...)
			sum := sha256.Sum256(body)
			source := nodeSource{image: "nodejs26.12.0", commit: commit, sha256: hex.EncodeToString(sum[:])}
			wantErr := test.wantErr
			if test.stale {
				source.sha256 = fmt.Sprintf("%x", sha256.Sum256([]byte("different source archive")))
				wantErr = fmt.Sprintf("nodejs26.12.0: Node archive SHA256 mismatch: got %x, want %s", sum, source.sha256)
			}
			deps, err := nodeNpmLocks(source, body)
			if wantErr != "" {
				if err == nil || err.Error() != wantErr {
					t.Fatalf("nodeNpmLocks() error = %v, want %q", err, wantErr)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got, want := len(deps), 2; got != want {
				t.Fatalf("dependency count = %d, want %d", got, want)
			}
			if got, want := []string{deps[0].name, deps[1].name}, []string{"npm/nodejs26.12.0/doc/node_modules/example", "npm/nodejs26.12.0/eslint/node_modules/example"}; !slices.Equal(got, want) {
				t.Errorf("cohort names = %v, want %v", got, want)
			}
		})
	}
}

func TestNpmLockIdentity(t *testing.T) {
	pin := npmTestIntegrity(nil)
	parent := `"version":"1.0.0","resolved":"https://registry.npmjs.org/example/-/example-1.0.0.tgz","integrity":"` + pin + `"`
	lock := `{"lockfileVersion":3,"packages":{"":{},"node_modules/example":{` + parent + `},"node_modules/example/node_modules/child":{"version":"2.0.0","inBundle":true},"node_modules/platform":{"version":"1.0.0","resolved":"https://registry.npmjs.org/platform/-/platform-1.0.0.tgz","integrity":"` + pin + `","optional":true,"os":["darwin"]}}}`
	deps, err := npmDependencies("nodejs26.12.0/doc", []byte(lock))
	if err != nil {
		t.Fatal(err)
	}
	if got, want := len(deps), 3; got != want {
		t.Fatalf("dependencies = %d, want %d including optional platform archive", got, want)
	}
	if got, want := deps[1].source(), deps[0].source()+"#node_modules/child"; got != want {
		t.Errorf("bundled source = %q, want %q", got, want)
	}
	if got, want := deps[1].integrity, deps[0].integrity; got != want {
		t.Errorf("bundled integrity = %q, want containing archive pin %q", got, want)
	}
	for _, test := range []struct{ lock, wantErr string }{
		{strings.ReplaceAll(lock, `"lockfileVersion":3`, `"lockfileVersion":2`), "expected npm lockfileVersion 3 with root and dependency packages"},
		{strings.ReplaceAll(lock, `"inBundle":true`, `"inBundle":true,"resolved":"https://example.com/other.tgz"`), "bundled package node_modules/example/node_modules/child also declares an independent archive"},
		{strings.ReplaceAll(lock, "example/node_modules/child", "absent/node_modules/child"), "bundled package node_modules/absent/node_modules/child has no containing lock entry node_modules/absent"},
		{strings.ReplaceAll(lock, pin, "sha512-invalid"), "node_modules/example: expected an HTTPS npm archive and SHA512 integrity pin"},
		{strings.ReplaceAll(lock, "node_modules/platform", "node_modules/../platform"), `invalid or unsupported npm package "node_modules/../platform"`},
	} {
		if _, err := npmDependencies("nodejs26.12.0/doc", []byte(test.lock)); err == nil || err.Error() != test.wantErr {
			t.Errorf("npmDependencies() error = %v, want %q", err, test.wantErr)
		}
	}
}

func TestNpmLicenseFetch(t *testing.T) {
	files := []npmTarFile{
		{"package/package.json", `{"name":"example","version":"1.0.0"}`},
		{"package/LICENSE", udiffBSD},
		{"package/node_modules/child/package.json", `{"name":"child","version":"2.0.0"}`},
		{"package/node_modules/child/README.md", udiffMIT},
	}
	body := npmTestArchive(t, files...)
	const archiveURL = "https://registry.npmjs.org/example/-/example-1.0.0.tgz"
	const noticeCommit = "0123456789abcdef0123456789abcdef01234567"
	const noticeURL = "https://raw.githubusercontent.com/example/upstream/" + noticeCommit + "/LICENSE"
	get := func(url string, _ map[string]string) ([]byte, error) {
		switch url {
		case archiveURL:
			return body, nil
		case noticeURL:
			return []byte(udiffMIT), nil
		default:
			return nil, fmt.Errorf("unexpected fetch %s", url)
		}
	}
	parent := dep{kind: kindNpm, url: archiveURL, integrity: npmTestIntegrity(body), npmName: "example", version: "1.0.0"}
	child := parent
	child.npmPath, child.npmName, child.version = "node_modules/child", "child", "2.0.0"
	entry := Entry{Version: parent.source(), SHA256: fmt.Sprintf("%x", sha256.Sum256(body)), Integrity: parent.integrity, LicenseSource: noticeURL}
	for _, test := range []struct {
		name       string
		d          dep
		root       string
		previous   Entry
		want       Licenses
		wantCommit string
	}{
		{"parent", parent, "package", Entry{}, Licenses{"BSD-3-Clause"}, ""},
		{"bundled child", child, "package", Entry{}, Licenses{"MIT"}, ""},
		{"named archive root", parent, "example", Entry{}, Licenses{"BSD-3-Clause"}, ""},
		{"bundled child under named root", child, "example", Entry{}, Licenses{"MIT"}, ""},
		{"explicit notice", parent, "package", entry, Licenses{"MIT"}, noticeCommit},
	} {
		t.Run(test.name, func(t *testing.T) {
			archiveFiles := slices.Clone(files)
			for i := range archiveFiles {
				archiveFiles[i].name = strings.Replace(archiveFiles[i].name, "package/", test.root+"/", 1)
			}
			body = npmTestArchive(t, archiveFiles...)
			test.d.integrity = npmTestIntegrity(body)
			result, err := fetchNpmLicense(test.d, test.previous, get)
			if err != nil {
				t.Fatal(err)
			}
			if !slices.Equal(result.license, test.want) {
				t.Errorf("licenses = %v, want %v", result.license, test.want)
			}
			if got, want := result.commit, test.wantCommit; got != want {
				t.Errorf("notice commit = %q, want %q", got, want)
			}
			if got, want := result.sha256, fmt.Sprintf("%x", sha256.Sum256(body)); got != want {
				t.Errorf("fetched SHA256 = %q, want %q", got, want)
			}
		})
	}
	// Corruption must fail before reading or classifying an explicit notice.
	body = append(slices.Clone(body), 0)
	for _, previous := range []Entry{{}, entry} {
		if _, err := fetchNpmLicense(parent, previous, func(url string, _ map[string]string) ([]byte, error) {
			if got, want := url, archiveURL; got != want {
				t.Errorf("read %q after archive corruption, want only %q", got, want)
			}
			return body, nil
		}); err == nil || err.Error() != "npm archive SHA512 integrity mismatch" {
			t.Errorf("changed archive error = %v, want integrity mismatch", err)
		}
	}
	for _, test := range []struct {
		files   []npmTarFile
		wantErr string
	}{
		{files[:1], "package/: no package license notice"},
		{append(slices.Clone(files), files[1]), "package/LICENSE: duplicate or nonregular npm notice/manifest"},
		{[]npmTarFile{{"package/package.json", `{"name":"different","version":"1.0.0"}`}, files[1]}, "package/: manifest different@1.0.0 differs from locked example@1.0.0"},
		{append(slices.Clone(files), npmTarFile{"other/LICENSE", udiffBSD}), `"other/LICENSE": npm archive has multiple roots`},
		{append(slices.Clone(files), npmTarFile{"package/../LICENSE", udiffBSD}), `"package/../LICENSE": invalid npm archive path`},
		{append(slices.Clone(files), npmTarFile{"/package/LICENSE", udiffBSD}), `"/package/LICENSE": invalid npm archive path`},
		{append(slices.Clone(files), npmTarFile{`package\LICENSE`, udiffBSD}), `"package\\LICENSE": invalid npm archive path`},
		{[]npmTarFile{{"package.json", `{"name":"example","version":"1.0.0"}`}}, `"package.json": npm archive entry has no leading directory`},
	} {
		body = npmTestArchive(t, test.files...)
		parent.integrity = npmTestIntegrity(body)
		if _, err := fetchNpmLicense(parent, Entry{}, get); err == nil || err.Error() != test.wantErr {
			t.Errorf("fetchNpmLicense() error = %v, want %q", err, test.wantErr)
		}
	}
}

func TestNpmAuditIntegrity(t *testing.T) {
	body := npmTestArchive(t, npmTarFile{"package/package.json", `{"name":"example","version":"1.0.0"}`}, npmTarFile{"package/LICENSE", udiffMIT})
	d := dep{name: "npm/nodejs26.12.0/doc/node_modules/example", kind: kindNpm, url: "https://registry.npmjs.org/example/-/example-1.0.0.tgz", integrity: npmTestIntegrity(body)}
	const commit = "0123456789abcdef0123456789abcdef01234567"
	e := Entry{
		Dependency: d.name, Version: d.source(), SHA256: fmt.Sprintf("%x", sha256.Sum256(body)),
		Integrity: d.integrity, Retrieved: "2026-10-20", License: Licenses{"MIT"}, ManuallyAudited: true,
		Commit: commit, LicenseSource: "https://raw.githubusercontent.com/example/upstream/" + commit + "/LICENSE",
	}
	if got, want := verifyProblems([]dep{d}, []Entry{e}), []string(nil); !slices.Equal(got, want) {
		t.Fatalf("unchanged audit problems = %v, want %v", got, want)
	}
	p := Paths{YAML: filepath.Join(t.TempDir(), "dependencies.yaml")}
	if err := writeEntries(p.YAML, []Entry{e}); err != nil {
		t.Fatal(err)
	}
	before, err := os.ReadFile(p.YAML)
	if err != nil {
		t.Fatal(err)
	}
	retrieve := func(d dep, _ Entry) (*fetched, error) {
		t.Error("retrieved manually audited terms")
		return nil, fmt.Errorf("unexpected retrieval for %s", d.name)
	}
	if err := fetch(p, []dep{d}, retrieve); err != nil {
		t.Fatal(err)
	}
	entries, err := readEntries(p.YAML)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := entries, []Entry{e}; !reflect.DeepEqual(got, want) {
		t.Errorf("retained manual audit = %+v, want %+v", got, want)
	}
	d.integrity = npmTestIntegrity([]byte("changed at same URL"))
	const sourceError = "license_source artifact identity changed or is unpinned; audit the new artifact and refresh its notice source"
	wantProblems := []string{
		d.name + ": " + sourceError,
		fmt.Sprintf("%s was audited with integrity %q, but is now pinned to %q", d.name, e.Integrity, d.integrity),
	}
	if got := verifyProblems([]dep{d}, []Entry{e}); !slices.Equal(got, wantProblems) {
		t.Errorf("changed audit problems = %v, want %v", got, wantProblems)
	}
	wantErr := "re-audit the license of " + d.name + ": " + strings.Join(wantProblems, "; ")
	if err := fetch(p, []dep{d}, retrieve); err == nil || err.Error() != wantErr {
		t.Errorf("changed manual audit error = %v, want %q", err, wantErr)
	}
	after, err := os.ReadFile(p.YAML)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before, after) {
		t.Error("fetch changed the manually audited catalog")
	}
}

func TestNpmFetchFailurePins(t *testing.T) {
	body := []byte("audited npm archive")
	d := dep{name: "npm/example/node_modules/example", kind: kindNpm, url: "https://registry.npmjs.org/example/-/example-1.0.0.tgz", integrity: npmTestIntegrity(body)}
	e := Entry{Dependency: d.name, Version: d.source(), SHA256: fmt.Sprintf("%x", sha256.Sum256(body)), Integrity: d.integrity, Retrieved: "2026-10-20", License: Licenses{"MIT"}}
	for _, test := range []struct {
		name, integrity, wantErr string
		want                     []Entry
	}{
		{"same pin", d.integrity, "", []Entry{e}},
		{"changed pin", npmTestIntegrity([]byte("new archive")), "cannot fetch licenses for " + d.name + "; fix the reported source or notice errors", nil},
	} {
		t.Run(test.name, func(t *testing.T) {
			p := Paths{YAML: filepath.Join(t.TempDir(), "dependencies.yaml")}
			if err := writeEntries(p.YAML, []Entry{e}); err != nil {
				t.Fatal(err)
			}
			d.integrity = test.integrity
			err := fetch(p, []dep{d}, func(dep, Entry) (*fetched, error) { return nil, fmt.Errorf("archive unavailable") })
			if test.wantErr != "" {
				if err == nil || err.Error() != test.wantErr {
					t.Fatalf("fetch error = %v, want %q", err, test.wantErr)
				}
			} else if err != nil {
				t.Fatal(err)
			}
			got, err := readEntries(p.YAML)
			if err != nil {
				t.Fatal(err)
			}
			if !slices.EqualFunc(got, test.want, func(a, b Entry) bool { return reflect.DeepEqual(a, b) }) {
				t.Errorf("retained catalog = %+v, want %+v", got, test.want)
			}
		})
	}
}

func TestNpmFetchCatalog(t *testing.T) {
	body := npmTestArchive(t, npmTarFile{"package/package.json", `{"name":"example","version":"1.0.0"}`}, npmTarFile{"package/LICENSE", udiffMIT})
	d := dep{
		name: "npm/example/node_modules/example", kind: kindNpm,
		url:       "https://registry.npmjs.org/example/-/example-1.0.0.tgz",
		integrity: npmTestIntegrity(body), npmName: "example", version: "1.0.0",
	}
	p := Paths{YAML: filepath.Join(t.TempDir(), "dependencies.yaml")}
	if err := fetch(p, []dep{d}, func(d dep, previous Entry) (*fetched, error) {
		return fetchNpmLicense(d, previous, func(string, map[string]string) ([]byte, error) { return body, nil })
	}); err != nil {
		t.Fatal(err)
	}
	entries, err := readEntries(p.YAML)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := len(entries), 1; got != want {
		t.Fatalf("catalog size = %d, want %d", got, want)
	}
	if got, want := entries[0].License, (Licenses{"MIT"}); !slices.Equal(got, want) {
		t.Errorf("catalog license = %v, want %v", got, want)
	}
	if got, want := verifyProblems([]dep{d}, entries), []string(nil); !slices.Equal(got, want) {
		t.Errorf("fetched catalog problems = %v, want %v", got, want)
	}
}
