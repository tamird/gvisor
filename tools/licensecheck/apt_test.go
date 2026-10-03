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
	_ "embed"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"testing"
)

// Captured from rules_distroless' resolved container_test_tools at c941e123c,
// using https://snapshot.ubuntu.com/ubuntu/20260928T000000Z. The lock fixture
// retains both selected closures and two unselected packages, base-files and
// the architecture-independent libsemanage-common.
// Copyright files are verbatim from the corresponding pinned AMD64 payload:
// busybox-static 1:1.30.1-7ubuntu3.1, erofs-utils 1.4-1, libpcre2-8-0 10.39-3ubuntu0.1.
var (
	//go:embed testdata/apt-lock.json
	aptLockText string
	//go:embed testdata/apt-busybox-static-copyright.txt
	aptBusyboxText string
	//go:embed testdata/apt-erofs-utils-copyright.txt
	aptEROFSText string
	//go:embed testdata/apt-libpcre2-8-0-copyright.txt
	aptPCREText string
)

func TestAptDependencies(t *testing.T) {
	// Exercise the actual escaped lock_content attribute in show_repo output.
	repos, err := parseShowRepos(fmt.Sprintf("## @tools:\ntranslate_dependency_set(\n  depset_name = %q,\n  lock_content = %q,\n)\n", "container_test_tools", aptLockText))
	if err != nil {
		t.Fatal(err)
	}
	deps, err := aptDependencies("tools", "@tools", repos["@tools"])
	if err != nil {
		t.Fatal(err)
	}
	if len(deps) != 20 {
		t.Fatalf("selected dependencies = %d, want 10 per target architecture", len(deps))
	}
	for _, arch := range []string{"amd64", "arm64"} {
		name := "apt/tools/" + arch + "/erofs-utils"
		i := slices.IndexFunc(deps, func(d dep) bool { return d.name == name })
		if i < 0 {
			t.Fatalf("missing %s", name)
		}
		d := deps[i]
		if d.kind != kindApt || d.aptRepo != "@tools" || d.aptArch != arch || d.aptPackage != "erofs-utils" ||
			d.source() != "https://snapshot.ubuntu.com/ubuntu/20260928T000000Z/pool/universe/e/erofs-utils/erofs-utils_1.4-1_"+arch+".deb" || len(d.sha256) != 64 {
			t.Errorf("selected package = %+v", d)
		}
	}
	for _, d := range deps {
		if d.aptPackage == "base-files" {
			t.Error("unselected package leaked from the shared lock into the hub")
		}
	}
}

func TestAptDependencyArchitectures(t *testing.T) {
	var lock aptLock
	if err := json.Unmarshal([]byte(aptLockText), &lock); err != nil {
		t.Fatal(err)
	}
	const root = "/jammy/erofs-utils:amd64=1.4-1"
	const independent = "/jammy/libsemanage-common:amd64=3.3-1build2"
	// A root's resolved edges can contain other target architectures. Only
	// its own CPU and Architecture: all contribute to its selected payload.
	pkg := lock.Packages[root]
	pkg.DependsOn = append(pkg.DependsOn, independent, "/jammy/erofs-utils:arm64=1.4-1")
	lock.Packages[root] = pkg
	selected := lock.DependencySets["container_test_tools"]
	delete(selected.Sets, "arm64")
	content, err := json.Marshal(lock)
	if err != nil {
		t.Fatal(err)
	}
	repo := repoInfo{rule: "translate_dependency_set", attrs: map[string][]string{"depset_name": {"container_test_tools"}, "lock_content": {string(content)}}}
	deps, err := aptDependencies("tools", "@tools", repo)
	if err != nil {
		t.Fatal(err)
	}
	if len(deps) != 11 {
		t.Fatalf("selected dependencies = %d, want 10 amd64 and one architecture-independent package", len(deps))
	}
	for _, d := range deps {
		if d.aptArch != "amd64" {
			t.Errorf("unexpected target architecture: %+v", d)
		}
	}
	if !slices.ContainsFunc(deps, func(d dep) bool { return d.aptPackage == "libsemanage-common" }) {
		t.Error("missing architecture-independent package")
	}
}

func TestAptInvalidInventory(t *testing.T) {
	const key = "/jammy/erofs-utils:amd64=1.4-1"
	for _, test := range []struct {
		name, problem string
		change        func(*aptLock)
	}{
		{"version", "unsupported lock version", func(l *aptLock) { l.Version = 3 }},
		{"missing set", "missing selected dependency set", func(l *aptLock) { delete(l.DependencySets, "container_test_tools") }},
		{"missing package", "missing or wrong-architecture", func(l *aptLock) { delete(l.Packages, key) }},
		{"missing edge", "missing dependency", func(l *aptLock) {
			p := l.Packages[key]
			p.DependsOn = append(p.DependsOn, "missing")
			l.Packages[key] = p
		}},
		{"unhashed", "SHA256 pin", func(l *aptLock) {
			p := l.Packages[key]
			p.SHA256 = ""
			l.Packages[key] = p
		}},
		{"wrong identity", "inconsistent package identity", func(l *aptLock) {
			p := l.Packages[key]
			p.Version = "2.0"
			l.Packages[key] = p
		}},
		{"credential", "without credentials", func(l *aptLock) {
			s := l.Sources["jammy"]
			s.URIs = []string{"https://user:secret@example.com"}
			l.Sources["jammy"] = s
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			var lock aptLock
			if err := json.Unmarshal([]byte(aptLockText), &lock); err != nil {
				t.Fatal(err)
			}
			test.change(&lock)
			content, err := json.Marshal(lock)
			if err != nil {
				t.Fatal(err)
			}
			repo := repoInfo{rule: "translate_dependency_set", attrs: map[string][]string{"depset_name": {"container_test_tools"}, "lock_content": {string(content)}}}
			if _, err := aptDependencies("tools", "@tools", repo); err == nil || !strings.Contains(err.Error(), test.problem) {
				t.Fatalf("error = %v, want %q", err, test.problem)
			}
		})
	}
}

func TestAptNotices(t *testing.T) {
	files := map[string]aptFile{
		"usr/share/doc/erofs-utils/copyright":    {text: aptEROFSText},
		"usr/share/doc/busybox-static/copyright": {text: aptBusyboxText},
		"usr/share/doc/libpcre2-8-0/copyright":   {text: aptPCREText},
	}
	if _, err := aptLicenseText(files, "erofs-utils"); err == nil || !strings.Contains(err.Error(), "copyright references /usr/share/common-licenses/") {
		t.Fatalf("missing common-license error = %v", err)
	}
	// Resolution preserves the entire raw notice, including grants and
	// exceptions, then includes each referenced common text exactly once.
	files["usr/share/common-licenses/GPL-2"] = aptFile{text: "GPL-2 common text"}
	files["usr/share/common-licenses/Apache-2.0"] = aptFile{text: "Apache-2.0 common text"}
	got, err := aptLicenseText(files, "erofs-utils")
	want := aptEROFSText + "\n\nApache-2.0 common text\n\nGPL-2 common text"
	if err != nil || got != want {
		t.Fatalf("resolved notice = %q, %v, want %q", got, err, want)
	}
	if got, err := aptLicenseText(files, "busybox-static"); err != nil || got != aptBusyboxText+"\n\nGPL-2 common text" {
		t.Errorf("BusyBox notice = %q, %v", got, err)
	}
	text, err := aptLicenseText(files, "libpcre2-8-0")
	if err != nil {
		t.Fatal(err)
	}
	licenses, err := classify(text)
	if err != nil || !slices.Equal(licenses, Licenses{"BSD-3-Clause"}) {
		t.Errorf("PCRE package notice = %v, %v", licenses, err)
	}
	// Actual GCC notices also end sentences with these unversioned names.
	files["usr/share/doc/example/copyright"] = aptFile{text: "See /usr/share/common-licenses/GPL. Also /usr/share/common-licenses/LGPL-2.1."}
	files["usr/share/common-licenses/GPL"] = aptFile{link: "usr/share/common-licenses/GPL-2"}
	files["usr/share/common-licenses/LGPL-2.1"] = aptFile{text: "LGPL-2.1 common text"}
	if _, err := aptLicenseText(files, "example"); err != nil {
		t.Errorf("sentence-ending punctuation: %v", err)
	}
}

func TestAptArchiveLinks(t *testing.T) {
	var data bytes.Buffer
	writer := tar.NewWriter(&data)
	for _, h := range []tar.Header{
		{Name: ".", Typeflag: tar.TypeDir},
		{Name: "./", Typeflag: tar.TypeDir},
		{Name: "./usr/share/doc/libgcc-s1", Typeflag: tar.TypeSymlink, Linkname: "./gcc-12-base"},
		{Name: "./usr/share/doc/gcc-12-base/copyright", Typeflag: tar.TypeReg, Size: int64(len(bsdText))},
		{Name: "./usr/share/doc/hard/copyright", Typeflag: tar.TypeLink, Linkname: "./usr/share/doc/gcc-12-base/copyright"},
	} {
		if err := writer.WriteHeader(&h); err != nil {
			t.Fatal(err)
		}
		if h.Typeflag == tar.TypeReg {
			if _, err := writer.Write([]byte(bsdText)); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	files, err := readAptArchive(&data)
	if err != nil {
		t.Fatal(err)
	}
	for _, pkg := range []string{"libgcc-s1", "hard"} {
		if text, err := aptLicenseText(files, pkg); err != nil || text != bsdText {
			t.Errorf("%s archive notice = %q, %v", pkg, text, err)
		}
	}
	files["usr/share/doc/gcc-12-base"] = aptFile{link: "usr/share/doc/libgcc-s1/nested"}
	if _, err := aptLicenseText(files, "libgcc-s1"); err == nil || !strings.Contains(err.Error(), "link cycle") {
		t.Errorf("expanding link-cycle error = %v", err)
	}
}
