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
	"slices"
	"strings"
	"testing"
)

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
	commit := strings.Repeat("a", 40)
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
		{"missing lock", files[:1], false, "missing tools/eslint/package-lock.json"},
		{"duplicate lock", append(slices.Clone(files), files[0]), false, "duplicate or nonregular lock"},
		{"overriding shrinkwrap", append(slices.Clone(files), npmTarFile{prefix + "doc/npm-shrinkwrap.json", lock}), false, "overrides the audited package lock"},
		{"changed archive", files, true, "Node archive SHA256 mismatch"},
	} {
		t.Run(test.name, func(t *testing.T) {
			body := npmTestArchive(t, test.files...)
			sum := sha256.Sum256(body)
			source := nodeSource{image: "nodejs26.12.0", commit: commit, sha256: hex.EncodeToString(sum[:])}
			if test.stale {
				source.sha256 = strings.Repeat("0", 64)
			}
			deps, err := nodeNpmLocks(source, body)
			if test.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), test.wantErr) {
					t.Fatalf("nodeNpmLocks() error = %v, want %q", err, test.wantErr)
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
		{strings.ReplaceAll(lock, `"lockfileVersion":3`, `"lockfileVersion":2`), "expected npm lockfileVersion 3"},
		{strings.ReplaceAll(lock, `"inBundle":true`, `"inBundle":true,"resolved":"https://example.com/other.tgz"`), "also declares an independent archive"},
		{strings.ReplaceAll(lock, "example/node_modules/child", "absent/node_modules/child"), "has no containing lock entry"},
		{strings.ReplaceAll(lock, pin, "sha512-invalid"), "expected an HTTPS npm archive and SHA512 integrity pin"},
		{strings.ReplaceAll(lock, "node_modules/platform", "node_modules/../platform"), "invalid or unsupported npm package"},
	} {
		if _, err := npmDependencies("nodejs26.12.0/doc", []byte(test.lock)); err == nil || !strings.Contains(err.Error(), test.wantErr) {
			t.Errorf("npmDependencies() error = %v, want %q", err, test.wantErr)
		}
	}
}

func TestNpmLicenseFetch(t *testing.T) {
	files := []npmTarFile{
		{"package/package.json", `{"name":"example","version":"1.0.0"}`},
		{"package/LICENSE", bsdText},
		{"package/node_modules/child/package.json", `{"name":"child","version":"2.0.0"}`},
		{"package/node_modules/child/README.md", udiffMIT},
	}
	body := npmTestArchive(t, files...)
	const archiveURL = "https://registry.npmjs.org/example/-/example-1.0.0.tgz"
	const noticeCommit = "0123456789abcdef0123456789abcdef01234567"
	const noticeURL = "https://raw.githubusercontent.com/example/upstream/" + noticeCommit + "/LICENSE"
	noticeReads := 0
	get := func(url string, _ map[string]string) ([]byte, error) {
		switch url {
		case archiveURL:
			return body, nil
		case noticeURL:
			noticeReads++
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
		previous   Entry
		want       Licenses
		wantCommit string
	}{
		{"parent", parent, Entry{}, Licenses{"BSD-3-Clause"}, ""},
		{"bundled child", child, Entry{}, Licenses{"MIT"}, ""},
		{"explicit notice", parent, entry, Licenses{"MIT"}, noticeCommit},
	} {
		t.Run(test.name, func(t *testing.T) {
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
			if got, want := result.sha256, entry.SHA256; got != want {
				t.Errorf("fetched SHA256 = %q, want %q", got, want)
			}
		})
	}
	// Corruption must fail before reading or classifying an explicit notice.
	body = append(slices.Clone(body), 0)
	for _, previous := range []Entry{{}, entry} {
		if _, err := fetchNpmLicense(parent, previous, get); err == nil || err.Error() != "npm archive SHA512 integrity mismatch" {
			t.Errorf("changed archive error = %v, want integrity mismatch", err)
		}
	}
	if got, want := noticeReads, 1; got != want {
		t.Errorf("notice reads = %d, want %d", got, want)
	}
	for _, test := range []struct {
		files   []npmTarFile
		wantErr string
	}{
		{files[:1], "no package license notice"},
		{append(slices.Clone(files), files[1]), "duplicate or nonregular npm notice/manifest"},
		{[]npmTarFile{{"package/package.json", `{"name":"different","version":"1.0.0"}`}, files[1]}, "differs from locked example@1.0.0"},
	} {
		body = npmTestArchive(t, test.files...)
		parent.integrity = npmTestIntegrity(body)
		if _, err := fetchNpmLicense(parent, Entry{}, get); err == nil || !strings.Contains(err.Error(), test.wantErr) {
			t.Errorf("fetchNpmLicense() error = %v, want %q", err, test.wantErr)
		}
	}
}

func TestNpmAuditIntegrity(t *testing.T) {
	d := dep{name: "npm/nodejs26.12.0/doc/node_modules/example", kind: kindNpm, url: "https://registry.npmjs.org/example/-/example-1.0.0.tgz", integrity: npmTestIntegrity(nil)}
	e := Entry{Dependency: d.name, Version: d.source(), SHA256: strings.Repeat("a", 64), Integrity: d.integrity, Retrieved: "2026-10-20", License: Licenses{"MIT"}, ManuallyAudited: true}
	if problems := verifyProblems([]dep{d}, []Entry{e}); len(problems) != 0 {
		t.Fatal(problems)
	}
	if !d.canRetain(e) {
		t.Fatal("cannot retain unchanged reviewed artifact")
	}
	e.LicenseSource = "https://raw.githubusercontent.com/example/upstream/" + strings.Repeat("b", 40) + "/LICENSE"
	if _, err := licenseSourceCommit(d, e); err != nil {
		t.Fatal(err)
	}
	d.integrity = npmTestIntegrity([]byte("changed at same URL"))
	if d.canRetain(e) {
		t.Error("retained stale integrity after a download failure")
	}
	if problems := verifyProblems([]dep{d}, []Entry{e}); !slices.ContainsFunc(problems, func(problem string) bool { return strings.Contains(problem, "integrity") }) {
		t.Errorf("Verify did not report integrity drift: %v", problems)
	}
	if _, err := pinnedManualEntries([]dep{d}, map[string]Entry{d.name: e}); err == nil || !strings.Contains(err.Error(), "integrity") {
		t.Errorf("manual audit error = %v, want integrity drift", err)
	}
	if _, err := licenseSourceCommit(d, e); err == nil || !strings.Contains(err.Error(), "artifact identity changed or is unpinned") {
		t.Errorf("explicit notice error = %v, want artifact drift", err)
	}
}
