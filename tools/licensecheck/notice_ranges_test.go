package licensecheck

import (
	"bytes"
	"crypto/sha256"
	"embed"
	"fmt"
	"slices"
	"testing"
)

//go:embed testdata/qualification-*.txt
var noticeFiles embed.FS

// TestNoticeRanges locates each identifier returned for the pinned inputs.
// Splitting at the appendix distinguishes the license terms from the specimen
// grant that the document invites other authors to put in their programs.
func TestNoticeRanges(t *testing.T) {
	scanner, err := licenseScanner()
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name, digest string
		want         Licenses
	}{
		{"qemu", "dd3ce02338c3a48abb6ba59b48809f7108a8bd242cb0cc8be90daafa30707c28", Licenses{"GPL-2.0", "GPL-2.0-or-later"}},
		{"ltp", "8177f97513213526df2cf6184d8ff986c675afb514d4e68a404010521b880643", Licenses{"GPL-2.0"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			body, err := noticeFiles.ReadFile("testdata/qualification-" + tc.name + ".txt")
			if err != nil {
				t.Fatal(err)
			}
			if got := fmt.Sprintf("%x", sha256.Sum256(body)); got != tc.digest {
				t.Fatalf("notice SHA-256 = %s, want %s", got, tc.digest)
			}
			cut := bytes.Index(body, []byte("How to Apply These Terms to Your New Programs"))
			if cut < 0 {
				t.Fatal("notice has no application appendix")
			}
			for _, m := range scanner.Scan(body).Match {
				if !m.IsURL {
					t.Logf("MATCH %s %s lines %d-%d", tc.name, m.ID, 1+bytes.Count(body[:m.Start], []byte("\n")), 1+bytes.Count(body[:m.End], []byte("\n")))
				}
			}
			full, err := classify(string(body))
			if err != nil || !slices.Equal(full, tc.want) {
				t.Errorf("full notice = %v, %v; catalog records %v", full, err, tc.want)
			}
			terms, err := classify(string(body[:cut]))
			if err != nil || !slices.Equal(terms, Licenses{"GPL-2.0"}) {
				t.Errorf("before appendix = %v, %v", terms, err)
			}
		})
	}
}
