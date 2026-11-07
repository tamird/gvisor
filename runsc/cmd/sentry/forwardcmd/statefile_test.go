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

package forwardcmd

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/google/subcommands"
	"golang.org/x/sys/unix"
	"google.golang.org/protobuf/proto"
	pgallocpb "gvisor.dev/gvisor/pkg/sentry/pgalloc/pgalloc_metadata_go_proto"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/statefile"
	"gvisor.dev/gvisor/pkg/state/wire"
	"gvisor.dev/gvisor/runsc/flag"
)

func TestStatefileDumpMemoryFiles(t *testing.T) {
	for _, layout := range []string{"inline", "split", "legacy", "generic", "same-metadata", "one-object", "missing-owners", "wrong-owners", "foreign-types", "truncated-metadata", "truncated-page", "raw-first", "generic-coincident-roots", "zero-owners", "missing-private", "extra-private", "invalid-owner-field"} {
		t.Run(layout, func(t *testing.T) {
			// Both keys were present when MemoryFile metadata first changed
			// to protobuf. Older Sentry checkpoints do not store CPU features.
			md := map[string]string{"runsc_version": "test", "container_count": "1"}
			if layout == "generic" {
				md = map[string]string{}
			}
			path := filepath.Join(t.TempDir(), "checkpoint")
			f, err := os.Create(path)
			if err != nil {
				t.Fatal(err)
			}
			w, err := statefile.NewWriter(f, nil, md)
			if err != nil {
				f.Close()
				t.Fatal(err)
			}
			ww := wire.Writer{Writer: w}
			object := func(tag wire.String, typeName string) {
				t.Helper()
				if err := state.WriteHeader(&ww, 1, true); err != nil {
					t.Fatal(err)
				}
				if typeName == "" {
					wire.Save(&ww, wire.Uint(1))
					wire.Save(&ww, &tag)
					return
				}
				wire.Save(&ww, &wire.Type{Name: typeName, Fields: []string{"marker"}})
				value := wire.Struct{TypeID: 1}
				value.Alloc(1)
				*value.Field(0) = &tag
				wire.Save(&ww, wire.Uint(1))
				wire.Save(&ww, &value)
			}
			owners := func(count int) {
				t.Helper()
				n := uint64(1)
				if count > 0 {
					n++
				}
				if err := state.WriteHeader(&ww, n, true); err != nil {
					t.Fatal(err)
				}
				wire.Save(&ww, &wire.Type{Name: "pkg/sentry/kernel.privateMemoryFileMetadata", Fields: []string{"owners"}})
				st := wire.Struct{TypeID: 1}
				st.Alloc(1)
				*st.Field(0) = wire.Nil{}
				if count > 0 {
					*st.Field(0) = &wire.Slice{Length: wire.Uint(count), Capacity: wire.Uint(count + 1), Ref: wire.Ref{Root: 2}}
				}
				wire.Save(&ww, wire.Uint(1))
				wire.Save(&ww, &st)
				if count > 0 {
					backing := wire.Array{}
					for i := 0; i <= count; i++ {
						backing.Contents = append(backing.Contents, wire.Nil{})
					}
					wire.Save(&ww, wire.Uint(2))
					wire.Save(&ww, &backing)
				}
			}
			var wantRaw []int
			memoryFile := func(pages int) {
				t.Helper()
				meta := pgallocpb.MemoryFileMetadataProto{Version: 1}
				if pages != 0 {
					meta.MemAcct = []*pgallocpb.MemAcctRangeProto{{End: uint64(pages), KnownCommitted: true}}
				}
				data, err := proto.Marshal(&meta)
				if err != nil {
					t.Fatal(err)
				}
				if err := binary.Write(w, binary.LittleEndian, uint64(len(data))); err != nil {
					t.Fatal(err)
				}
				if _, err := w.Write(data); err != nil {
					t.Fatal(err)
				}
				wantRaw = append(wantRaw, len(data))
				if pages != 0 {
					if err := state.WriteHeader(&ww, uint64(pages), false); err != nil {
						t.Fatal(err)
					}
					if _, err := w.Write(make([]byte, pages)); err != nil {
						t.Fatal(err)
					}
					wantRaw = append(wantRaw, pages)
				}
			}
			if layout == "raw-first" {
				if err := state.WriteHeader(&ww, 0, false); err != nil {
					t.Fatal(err)
				}
			}
			if layout == "foreign-types" {
				object("cpu", "example.org/pkg/cpuid.FeatureSet")
				object("kernel", "example.org/pkg/sentry/kernel.Kernel")
			} else if layout == "generic" || layout == "same-metadata" {
				object("cpu", "")
				object("kernel", "")
			} else {
				object("cpu", "pkg/cpuid.FeatureSet")
				if layout != "one-object" {
					object("kernel", "pkg/sentry/kernel.Kernel")
				}
			}
			switch layout {
			case "inline":
				memoryFile(4096)
				owners(2)
				memoryFile(0)
				memoryFile(8192)
			case "legacy", "generic", "same-metadata", "foreign-types", "raw-first":
				if layout == "same-metadata" || layout == "foreign-types" || layout == "raw-first" {
					// This is an ordinary raw state region.
					if err := state.WriteHeader(&ww, 2, false); err != nil {
						t.Fatal(err)
					}
					if _, err := w.Write([]byte("ok")); err != nil {
						t.Fatal(err)
					}
					wantRaw = append(wantRaw, 2)
				}
				object("saved memoryFile", "")
				object("private owners", "")
			case "generic-coincident-roots":
				for range 8 {
					if err := state.WriteHeader(&ww, 0, false); err != nil {
						t.Fatal(err)
					}
				}
				object("private owners", "") // An ordinary graph must still be reached.
			case "zero-owners":
				memoryFile(0)
				owners(0)
			case "missing-private":
				memoryFile(0)
				owners(2)
				memoryFile(0)
			case "extra-private":
				memoryFile(0)
				owners(1)
				memoryFile(0)
				memoryFile(0)
			case "invalid-owner-field":
				memoryFile(0)
				object("private owners", "pkg/sentry/kernel.privateMemoryFileMetadata")
			case "missing-owners":
				memoryFile(0)
			case "wrong-owners":
				memoryFile(0)
				object("private owners", "user.privateMemoryFileMetadata")
			case "truncated-metadata":
				if err := binary.Write(w, binary.LittleEndian, uint64(1)<<40); err != nil {
					t.Fatal(err)
				}
			case "truncated-page":
				data, err := proto.Marshal(&pgallocpb.MemoryFileMetadataProto{Version: 1, MemAcct: []*pgallocpb.MemAcctRangeProto{{End: 4096, KnownCommitted: true}}})
				if err != nil {
					t.Fatal(err)
				}
				if err := binary.Write(w, binary.LittleEndian, uint64(len(data))); err != nil {
					t.Fatal(err)
				}
				if _, err := w.Write(data); err != nil {
					t.Fatal(err)
				}
				if err := state.WriteHeader(&ww, 4096, false); err != nil {
					t.Fatal(err)
				}
				if _, err := w.Write([]byte("short")); err != nil {
					t.Fatal(err)
				}
			}
			if err := w.Close(); err != nil {
				t.Fatal(err)
			}
			for _, html := range []bool{false, true} {
				reader, err := os.Open(path)
				if err != nil {
					t.Fatal(err)
				}
				var out bytes.Buffer
				checkpoint := true
				switch layout {
				case "generic", "same-metadata", "one-object", "foreign-types", "raw-first", "generic-coincident-roots":
					checkpoint = false
				}
				err = (&Statefile{html: html, checkpoint: checkpoint}).writeTo(&out, reader)
				reader.Close()
				wantErr := map[string]string{
					"wrong-owners":        "unexpected checkpoint root",
					"invalid-owner-field": "invalid private MemoryFile owners slice",
					"missing-private":     "private MemoryFile 2 of 2:",
					"extra-private":       "unexpected data after the private MemoryFiles",
				}[layout]
				if wantErr != "" {
					if err == nil || !strings.Contains(err.Error(), wantErr) {
						t.Fatalf("got %v, want %q", err, wantErr)
					}
					continue
				}
				if layout == "truncated-page" {
					if !errors.Is(err, io.EOF) && !errors.Is(err, io.ErrUnexpectedEOF) {
						t.Fatalf("truncated page: %v", err)
					}
					continue
				}
				if layout == "missing-owners" || layout == "truncated-metadata" {
					if !errors.Is(err, io.ErrUnexpectedEOF) {
						t.Errorf("dump with missing private owners graph: %v", err)
					}
					continue
				}
				if err != nil {
					t.Fatalf("dump html=%t: %v", html, err)
				}
				text := out.String()
				hasOwners := strings.Contains(text, "private owners") || strings.Contains(text, "privateMemoryFileMetadata")
				if !strings.Contains(text, "cpu") || strings.Contains(text, "kernel") != (layout != "one-object") ||
					(hasOwners != (layout != "split" && layout != "one-object")) {
					t.Fatalf("missing or unexpected graph, html=%t: %s", html, text)
				}
				var raw []int
				for _, line := range strings.Split(text, "\n") {
					line = strings.TrimPrefix(line, "<pre>")
					if strings.Contains(line, " bytes non-object data)") {
						var size int
						if _, err := fmt.Sscanf(line, "(%d bytes non-object data)", &size); err != nil {
							t.Fatal(err)
						}
						raw = append(raw, size)
					}
				}
				if !slices.Equal(raw, wantRaw) {
					t.Errorf("raw regions = %v, want %v", raw, wantRaw)
				}
			}
		})
	}
}

func TestCheckStickyDirOwnership(t *testing.T) {
	stickyWorld := &unix.Stat_t{Mode: unix.S_IFDIR | unix.S_ISVTX | 0777, Uid: 0}
	stickyNotWorld := &unix.Stat_t{Mode: unix.S_IFDIR | unix.S_ISVTX | 0775, Uid: 0}
	nonStickyWorld := &unix.Stat_t{Mode: unix.S_IFDIR | 0777, Uid: 0}

	if err := checkStickyDirOwnership(stickyWorld, 1000, 0); !errors.Is(err, unix.EACCES) {
		t.Errorf("root following uid=1000 entry in root-owned 1777 dir: got %v, want EACCES", err)
	}
	if err := checkStickyDirOwnership(stickyWorld, 1000, 2000); !errors.Is(err, unix.EACCES) {
		t.Errorf("uid=2000 following uid=1000 entry in root-owned 1777 dir: got %v, want EACCES", err)
	}
	if err := checkStickyDirOwnership(stickyWorld, 1000, 1000); err != nil {
		t.Errorf("owner following own entry in 1777 dir: got %v, want nil", err)
	}
	if err := checkStickyDirOwnership(stickyWorld, 0, 1000); err != nil {
		t.Errorf("following dir-owner entry in 1777 dir: got %v, want nil", err)
	}
	if err := checkStickyDirOwnership(stickyNotWorld, 1000, 0); err != nil {
		t.Errorf("following entry in non-world-writable sticky dir: got %v, want nil", err)
	}
	if err := checkStickyDirOwnership(nonStickyWorld, 1000, 0); err != nil {
		t.Errorf("following entry in non-sticky dir: got %v, want nil", err)
	}
}

func TestSplitParentAndBase(t *testing.T) {
	for _, tc := range []struct {
		in            string
		wantParent    string
		wantBase      string
		wantHasParent bool
		wantTrailing  bool
	}{
		{in: "file.txt", wantParent: "", wantBase: "file.txt", wantHasParent: false, wantTrailing: false},
		{in: "/file.txt", wantParent: "/", wantBase: "file.txt", wantHasParent: true, wantTrailing: false},
		{in: "link/../checkpoint", wantParent: "link/..", wantBase: "checkpoint", wantHasParent: true, wantTrailing: false},
		{in: "../checkpoint", wantParent: "..", wantBase: "checkpoint", wantHasParent: true, wantTrailing: false},
		{in: "dir/", wantParent: "", wantBase: "", wantHasParent: false, wantTrailing: true},
		{in: "/", wantParent: "", wantBase: "", wantHasParent: false, wantTrailing: true},
	} {
		parent, base, hasParent, trailing := splitParentAndBase(tc.in)
		if parent != tc.wantParent || base != tc.wantBase || hasParent != tc.wantHasParent || trailing != tc.wantTrailing {
			t.Errorf("splitParentAndBase(%q) = (%q, %q, %v, %v), want (%q, %q, %v, %v)",
				tc.in, parent, base, hasParent, trailing,
				tc.wantParent, tc.wantBase, tc.wantHasParent, tc.wantTrailing)
		}
	}
}

func TestWriteOutputFileSymlinkDotDotTraversal(t *testing.T) {
	root := t.TempDir()
	workDir := filepath.Join(root, "work")
	otherChild := filepath.Join(root, "other", "child")
	if err := os.MkdirAll(workDir, 0755); err != nil {
		t.Fatalf("MkdirAll(workDir): %v", err)
	}
	if err := os.MkdirAll(otherChild, 0755); err != nil {
		t.Fatalf("MkdirAll(otherChild): %v", err)
	}
	// work/link -> ../other/child, so work/link/../checkpoint resolves in the
	// kernel to root/other/checkpoint, NOT work/checkpoint.
	if err := os.Symlink(filepath.Join("..", "other", "child"), filepath.Join(workDir, "link")); err != nil {
		t.Fatalf("Symlink: %v", err)
	}

	inputPath := filepath.Join(workDir, "checkpoint")
	wantInput := []byte("original-input-checkpoint")
	if err := os.WriteFile(inputPath, wantInput, 0644); err != nil {
		t.Fatalf("WriteFile(input): %v", err)
	}
	inFile, err := os.Open(inputPath)
	if err != nil {
		t.Fatalf("Open(input): %v", err)
	}
	defer inFile.Close()
	var inStat unix.Stat_t
	if err := unix.Fstat(int(inFile.Fd()), &inStat); err != nil {
		t.Fatalf("Fstat(input): %v", err)
	}

	outExpr := filepath.Join(workDir, "link") + "/../checkpoint"
	if err := writeOutputFile(outExpr, &inStat, func(f *os.File) error {
		_, werr := f.WriteString("decoded-output\n")
		return werr
	}); err != nil {
		t.Fatalf("writeOutputFile(%q) failed: %v", outExpr, err)
	}

	gotInput, err := os.ReadFile(inputPath)
	if err != nil {
		t.Fatalf("ReadFile(input): %v", err)
	}
	if string(gotInput) != string(wantInput) {
		t.Errorf("input file %s was clobbered: got %q, want %q", inputPath, gotInput, wantInput)
	}

	resolvedOut := filepath.Join(root, "other", "checkpoint")
	gotOut, err := os.ReadFile(resolvedOut)
	if err != nil {
		t.Fatalf("ReadFile(resolvedOut): %v", err)
	}
	if string(gotOut) != "decoded-output\n" {
		t.Errorf("resolved output %s = %q, want %q", resolvedOut, gotOut, "decoded-output\n")
	}
}

func TestStatefileExecuteDirectoryWithoutReadPerm(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root bypasses DAC directory read permission check")
	}

	root := t.TempDir()
	imgPath := filepath.Join(root, "meta.img")
	f, err := os.Create(imgPath)
	if err != nil {
		t.Fatalf("Create(imgPath): %v", err)
	}
	w, err := statefile.NewWriter(f, nil, map[string]string{"k": "v"})
	if err != nil {
		f.Close()
		t.Fatalf("NewWriter: %v", err)
	}
	if _, err := w.Write([]byte("unused")); err != nil {
		w.Close()
		t.Fatalf("Write: %v", err)
	}
	if err := w.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	runStatefile := func(args ...string) subcommands.ExitStatus {
		var cmd Statefile
		fs := flag.NewFlagSet("statefile", flag.ContinueOnError)
		cmd.SetFlags(fs)
		if err := fs.Parse(args); err != nil {
			t.Fatalf("fs.Parse(%v): %v", args, err)
		}
		return cmd.Execute(context.Background(), fs)
	}

	wxDir := filepath.Join(root, "wx_only")
	if err := os.Mkdir(wxDir, 0700); err != nil {
		t.Fatalf("Mkdir(wxDir): %v", err)
	}
	t.Cleanup(func() { _ = os.Chmod(wxDir, 0700) })
	if err := os.Chmod(wxDir, 0300); err != nil {
		t.Fatalf("Chmod(wxDir, 0300): %v", err)
	}

	wxOut := filepath.Join(wxDir, "out.txt")
	if status := runStatefile("-get=k", "-output="+wxOut, imgPath); status != subcommands.ExitSuccess {
		t.Errorf("Statefile.Execute in 0300 dir = %v, want ExitSuccess", status)
	} else if got, err := os.ReadFile(wxOut); err != nil {
		t.Fatalf("ReadFile(%s): %v", wxOut, err)
	} else if string(got) != "v\n" {
		t.Errorf("ReadFile(%s) = %q, want %q", wxOut, got, "v\n")
	}

	origWD, err := os.Getwd()
	if err != nil {
		t.Fatalf("Getwd: %v", err)
	}
	if err := os.Chdir(wxDir); err != nil {
		t.Fatalf("Chdir(wxDir): %v", err)
	}
	defer func() { _ = os.Chdir(origWD) }()
	if status := runStatefile("-get=k", "-output=cwd_out.txt", imgPath); status != subcommands.ExitSuccess {
		t.Errorf("Statefile.Execute in 0300 cwd = %v, want ExitSuccess", status)
	} else if got, err := os.ReadFile(filepath.Join(wxDir, "cwd_out.txt")); err != nil {
		t.Fatalf("ReadFile(cwd_out.txt): %v", err)
	} else if string(got) != "v\n" {
		t.Errorf("ReadFile(cwd_out.txt) = %q, want %q", got, "v\n")
	}
	if err := os.Chdir(origWD); err != nil {
		t.Fatalf("Chdir(origWD): %v", err)
	}

	realDir := filepath.Join(root, "real")
	if err := os.Mkdir(realDir, 0700); err != nil {
		t.Fatalf("Mkdir(realDir): %v", err)
	}
	xDir := filepath.Join(root, "x_only")
	if err := os.Mkdir(xDir, 0700); err != nil {
		t.Fatalf("Mkdir(xDir): %v", err)
	}
	linkPath := filepath.Join(xDir, "outlink")
	targetPath := filepath.Join(realDir, "linked.txt")
	if err := os.Symlink(targetPath, linkPath); err != nil {
		t.Fatalf("Symlink: %v", err)
	}
	t.Cleanup(func() { _ = os.Chmod(xDir, 0700) })
	if err := os.Chmod(xDir, 0100); err != nil {
		t.Fatalf("Chmod(xDir, 0100): %v", err)
	}

	if status := runStatefile("-get=k", "-output="+linkPath, imgPath); status != subcommands.ExitSuccess {
		t.Errorf("Statefile.Execute through 0100 dir symlink = %v, want ExitSuccess", status)
	} else if got, err := os.ReadFile(targetPath); err != nil {
		t.Fatalf("ReadFile(%s): %v", targetPath, err)
	} else if string(got) != "v\n" {
		t.Errorf("ReadFile(%s) = %q, want %q", targetPath, got, "v\n")
	}
}
