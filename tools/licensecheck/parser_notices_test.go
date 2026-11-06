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
	"crypto/sha256"
	_ "embed"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// These are the exact notice bytes previously compared with the llvm-redist
// release archive. Only the documents under test are present in the sandbox.
var (
	//go:embed testdata/parser-root.txt
	parserRoot string
	//go:embed testdata/parser-regex.txt
	parserRegex string
	//go:embed testdata/parser-blake3.txt
	parserBlake3 string
)

func TestParserArchiveNotices(t *testing.T) {
	const archive = "https://github.com/hermeticbuild/llvm-redist/releases/download/llvmorg-23.1.2-r1/llvm-project-23.1.2.src.tar.zst"
	const digest = "271da53a2feb0cb221a9276a14268a3b76ad802c7fc8fb1098291a43b06e4c56"
	dependency := dep{name: "llvm-project", kind: kindArchive, url: archive, sha256: digest}
	entry := Entry{
		Dependency: dependency.name,
		Version:    archive,
		Retrieved:  "2026-11-05",
		Commit:     "85ac560262434c9ccfc0c183ec22d4138ed647fb",
		SHA256:     digest,
		License:    Licenses{"Apache-2.0 WITH LLVM-exception", "BSD-3-Clause", "CC0-1.0", "NCSA", "Spencer-94"},
	}
	type result struct {
		Source string
		SHA256 string
		IDs    Licenses
		Error  string
	}
	var notices []result
	for _, n := range []struct{ name, text, sha string }{
		{"LICENSE.TXT", parserRoot, "8d85c1057d742e597985c7d4e6320b015a9139385cff4cbae06ffc0ebe89afee"},
		{"llvm/lib/Support/COPYRIGHT.regex", parserRegex, "0424e57d4303164dc59a8509c20dae0518b853692e5c2b0e98b11816fdbc97c7"},
		{"llvm/lib/Support/BLAKE3/LICENSE", parserBlake3, "6a94bedb8b707ed97f6e310d0d015ab14e0683ffa0a612b02958581b9cc9fc0e"},
	} {
		if got := fmt.Sprintf("%x", sha256.Sum256([]byte(n.text))); got != n.sha {
			t.Fatalf("%s SHA-256 = %s, want %s", n.name, got, n.sha)
		}
		ids, err := classify(n.text)
		r := result{Source: n.name, SHA256: n.sha, IDs: ids}
		if err != nil {
			r.Error = err.Error()
		}
		notices = append(notices, r)
	}
	problems := verifyProblems([]dep{dependency}, []Entry{entry})
	changed := dependency
	changed.sha256 = strings.Repeat("0", 64)
	changedProblems := verifyProblems([]dep{changed}, []Entry{entry})
	receipt := struct {
		ArchiveSHA256 string
		Notices       []result
		ReviewedTerms Licenses
		Problems      []string
		ChangedPin    []string
	}{digest, notices, entry.License, problems, changedProblems}
	data, err := json.MarshalIndent(receipt, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR"), "recognition.json"), data, 0644); err != nil {
		t.Fatal(err)
	}
	if len(changedProblems) == 0 {
		t.Fatal("Verify accepted the altered archive pin")
	}
}
