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

package cmd

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"

	"gvisor.dev/gvisor/pkg/sentry/pgalloc/metadata"
	"gvisor.dev/gvisor/pkg/sentry/state/checkpointfiles"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/statefile"
	"gvisor.dev/gvisor/pkg/state/wire"
	"gvisor.dev/gvisor/pkg/test/testutil"
	"gvisor.dev/gvisor/runsc/config"
	"gvisor.dev/gvisor/runsc/specutils"
)

type compatRelease struct {
	cli, sidecars string
}

// Each command gets a fresh process and exactly one matching CLI/sidecar pair.
// Real files also prevent a detached guest holding a command's output pipe open.
func (r compatRelease) invoke(t *testing.T, conf *config.Config, args ...string) ([]byte, []byte, error) {
	t.Helper()
	cmd := exec.Command(r.cli, append(conf.ToFlags(), args...)...)
	for _, entry := range os.Environ() {
		if !strings.HasPrefix(entry, "GVISOR_SIDECAR_BINARIES_DIR=") {
			cmd.Env = append(cmd.Env, entry)
		}
	}
	cmd.Env = append(cmd.Env, "GVISOR_SIDECAR_BINARIES_DIR="+r.sidecars)
	out, err := os.CreateTemp(t.TempDir(), "stdout")
	if err != nil {
		t.Fatal(err)
	}
	defer out.Close()
	diag, err := os.CreateTemp(t.TempDir(), "stderr")
	if err != nil {
		t.Fatal(err)
	}
	defer diag.Close()
	cmd.Stdout, cmd.Stderr = out, diag
	exit := testutil.RunCmd(cmd)
	stderr, err := os.ReadFile(diag.Name())
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(out.Name())
	if err != nil {
		t.Fatal(err)
	}
	return data, stderr, exit
}

func (r compatRelease) command(t *testing.T, conf *config.Config, args ...string) []byte {
	t.Helper()
	data, stderr, err := r.invoke(t, conf, args...)
	if err != nil {
		t.Fatalf("%s: %v: %s", args[0], err, stderr)
	}
	return data
}

func compatDigest(t *testing.T, path string) [32]byte {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		t.Fatal(err)
	}
	return [32]byte(h.Sum(nil))
}

func compatEnvelope(t *testing.T, path string) (statefile.Reader, map[string]string) {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	r, md, err := statefile.NewReader(f, nil)
	if err != nil {
		f.Close()
		t.Fatal(err)
	}
	return r, md
}

// Derive the expected region list from the actual original file independently
// of either CLI's print loop, pgalloc.InspectInline and its page-header helper.
// The only protobuf operation shared with the product is schema decoding.
func checkpointLayout(t *testing.T, path string, split, graph bool) ([]int64, int) {
	t.Helper()
	src, md := compatEnvelope(t, path)
	defer src.Close()
	if md["runsc_version"] != "qualification-memoryfile-compat" {
		t.Fatal("wrong source stamp")
	}
	r := bufio.NewReader(src)
	wr := wire.Reader{Reader: r}
	object := func() {
		t.Helper()
		h, err := state.ReadHeaderMode(&wr)
		if err != nil || !h.Object || h.Graph != graph {
			t.Fatalf("unexpected original graph header: %+v %v", h, err)
		}
		if h.Graph {
			if _, err := io.CopyN(io.Discard, r, int64(h.Length)); err != nil {
				t.Fatal(err)
			}
			return
		}
		for i := uint64(0); i < h.Length; {
			switch field := wire.Load(&wr).(type) {
			case *wire.Type:
			case wire.Uint:
				_ = wire.Load(&wr)
				i++
			default:
				t.Fatalf("unexpected saved object ID type %T", field)
			}
		}
	}
	object()
	object()
	var want []int64
	files := 0
	if !split {
		memoryFile := func() {
			t.Helper()
			var n uint64
			if err := binary.Read(r, binary.LittleEndian, &n); err != nil {
				t.Fatal(err)
			}
			if n < 2 || n > 64<<20 {
				t.Fatalf("unexpected fixture metadata length %d", n)
			}
			raw, err := io.ReadAll(io.LimitReader(r, int64(n)))
			if err != nil || uint64(len(raw)) != n {
				t.Fatal("short fixture protobuf")
			}
			pb, err := metadata.Decode(raw)
			if err != nil {
				t.Fatal(err)
			}
			want = append(want, int64(n))
			for _, seg := range pb.MemAcct {
				if !seg.KnownCommitted {
					continue
				}
				h, err := state.ReadHeaderMode(&wr)
				if err != nil || h.Object || seg.End <= seg.Start || h.Length != seg.End-seg.Start {
					t.Fatal("wrong original page header")
				}
				if _, err := io.CopyN(io.Discard, r, int64(h.Length)); err != nil {
					t.Fatal(err)
				}
				want = append(want, int64(h.Length))
			}
			files++
		}
		memoryFile()
		object()
		for {
			_, err := r.Peek(1)
			if err == io.EOF {
				break
			}
			if err != nil {
				t.Fatal(err)
			}
			memoryFile()
		}
		if files < 2 {
			t.Fatalf("checkpoint has no private MemoryFile")
		}
	}
	if _, err := r.Peek(1); err != io.EOF {
		t.Fatalf("unexpected trailing data: %v", err)
	}
	if err := src.Verify(); err != nil {
		t.Fatal(err)
	}
	return want, files
}

func printedLayout(data []byte) []int64 {
	var lengths []int64
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimPrefix(line, "<pre>")
		value, found := strings.CutSuffix(line, " bytes non-object data)")
		if !found || !strings.HasPrefix(value, "(") {
			continue
		}
		n, err := strconv.ParseInt(value[1:], 10, 64)
		if err == nil {
			lengths = append(lengths, n)
		}
	}
	return lengths
}

func TestExistingCheckpointReader(t *testing.T) {
	stop := testutil.StartReaper()
	defer stop()
	oldDir := os.Getenv("GVISOR_CKPT_OLD_RELEASE")
	if oldDir == "" {
		t.Fatal("stock release missing")
	}
	stock := compatRelease{filepath.Join(oldDir, "runsc"), filepath.Join(oldDir, "gvisor-bin")}
	fixed := compatRelease{specutils.ExePath, os.Getenv("GVISOR_SIDECAR_BINARIES_DIR")}
	tree := []string{"--target-dir=/witness", "--depth=1", "--file-per-level=2", "--file-size=8192", "--seed=31"}
	for _, kind := range []string{"wire", "graph", "split", "filesystem"} {
		t.Run(kind, func(t *testing.T) {
			conf := testutil.TestConfig(t)
			conf.Network = config.NetworkNone
			conf.StateEngine = config.StateEngineWire
			if kind == "graph" {
				conf.StateEngine = config.StateEngineGraph
			}
			if err := conf.Overlay2.Set("root:self"); err != nil {
				t.Fatal(err)
			}
			spec := idleGuestSpec(t)
			spec.Root.Readonly = false
			spec.Annotations = map[string]string{specutils.ContainerdContainerNameAnnotation: "checkpoint-root"}
			_, bundle, cleanup, err := testutil.SetupContainer(spec, conf)
			if err != nil {
				t.Fatal(err)
			}
			defer cleanup()
			for _, rel := range []compatRelease{stock, fixed} {
				out := rel.command(t, conf, "--version")
				if strings.SplitN(string(out), "\n", 2)[0] != "runsc version qualification-memoryfile-compat" {
					t.Fatal("wrong version")
				}
			}
			srcID, dstID := testutil.RandomContainerID(), testutil.RandomContainerID()
			stock.command(t, conf, "create", "--bundle="+bundle, srcID)
			stock.command(t, conf, "start", srcID)
			stock.command(t, conf, append([]string{"exec", srcID, "/test_app", "fsTreeCreate"}, tree...)...)
			dir := t.TempDir()
			if kind == "filesystem" {
				stock.command(t, conf, "fscheckpoint", "--image-path="+dir, srcID)
			} else {
				compression := "flate-best-speed"
				if kind == "split" {
					compression = "none"
				}
				stock.command(t, conf, "checkpoint", "--compression="+compression, "--image-path="+dir, srcID)
			}
			stock.command(t, conf, "delete", "--force", srcID)
			if kind == "filesystem" {
				spec2 := idleGuestSpec(t)
				spec2.Root.Readonly = false
				spec2.Annotations = spec.Annotations
				bundle2, clean, err := testutil.SetupBundleDir(spec2)
				if err != nil {
					t.Fatal(err)
				}
				defer clean()
				fixed.command(t, conf, "create", "--bundle="+bundle2, "--fs-restore-image-path="+dir, dstID)
				fixed.command(t, conf, "start", dstID)
			} else {
				image := filepath.Join(dir, checkpointfiles.StateFileName)
				sum := compatDigest(t, image)
				want, count := checkpointLayout(t, image, kind == "split", kind == "graph")
				for _, mode := range []string{"text", "html"} {
					args := []string{"statefile"}
					if mode == "html" {
						args = append(args, "--html")
					}
					args = append(args, image)
					before, _, oldErr := stock.invoke(t, conf, args...)
					after := fixed.command(t, conf, args...)
					split := kind == "split"
					if !slices.Equal(printedLayout(after), want) || bytes.Contains(after, []byte("privateMemoryFileMetadata")) == split {
						t.Fatalf("incorrect %s dump layout (%d sections, expected %d)", mode, len(printedLayout(after)), len(want))
					}
					if mode == "html" && (!bytes.HasPrefix(after, []byte("<html><body>\n<pre>")) || !bytes.Contains(after, []byte("</pre></body></html>"))) {
						t.Fatal("HTML framing")
					}
					verdict := "match"
					if oldErr != nil {
						verdict = "error"
					} else if !slices.Equal(printedLayout(before), want) || bytes.Contains(before, []byte("privateMemoryFileMetadata")) == split {
						verdict = "wrong-boundaries"
					}
					if split && (verdict != "match" || !bytes.Equal(before, after)) {
						t.Fatal("ordinary split rendering changed")
					}
					first, oldFirst := int64(-1), int64(-1)
					if len(want) > 0 {
						first = want[0]
					}
					oldSizes := printedLayout(before)
					if len(oldSizes) > 0 {
						oldFirst = oldSizes[0]
					}
					t.Logf("CHECKPOINT_DUMP layout=%s mode=%s old=%s image=%x regions=%d files=%d first=%d old_first=%d old_regions=%d expected=%x rendered=%x", kind, mode, verdict, sum, len(want), count, first, oldFirst, len(oldSizes), sha256.Sum256([]byte(fmt.Sprint(want))), sha256.Sum256(after))
				}
				fixed.command(t, conf, "restore", "--detach", "--background=false", "--direct-io=off", "--bundle="+bundle, "--image-path="+dir, dstID)
				if compatDigest(t, image) != sum {
					t.Fatal("changed checkpoint")
				}
				t.Logf("CHECKPOINT_RESTORED layout=%s image=%x", kind, sum)
			}
			defer fixed.command(t, conf, "delete", "--force", dstID)
			if got := fixed.command(t, conf, "ps", "--format=json", dstID); strings.TrimSpace(string(got)) != "[1]" {
				t.Fatalf("restored PIDs: %q", got)
			}
			tab := strings.Split(strings.TrimSpace(string(fixed.command(t, conf, "ps", dstID))), "\n")
			if len(tab) != 2 {
				t.Fatal("wrong process count")
			}
			cols := strings.Fields(tab[1])
			if len(cols) != 9 || cols[1] != "1" || cols[8] != "test_app" {
				t.Fatal("wrong restored process")
			}
			fixed.command(t, conf, append([]string{"exec", dstID, "/test_app", "fsTreeVerify"}, tree...)...)
			t.Logf("OLD_GUEST layout=%s pid=1 files=2 bytes_each=8192", kind)
		})
	}
}
