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
	"encoding/binary"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"
	pgallocpb "gvisor.dev/gvisor/pkg/sentry/pgalloc/pgalloc_metadata_go_proto"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/pretty"
	"gvisor.dev/gvisor/pkg/state/statefile"
	"gvisor.dev/gvisor/pkg/state/wire"
)

// checkpointObject encodes one wire struct and any objects its field references.
func checkpointObject(t *testing.T, name, field string, value wire.Object, referenced ...wire.Object) []byte {
	t.Helper()
	var buf bytes.Buffer
	w := wire.Writer{Writer: &buf}
	if err := state.WriteHeader(&w, uint64(1+len(referenced)), true); err != nil {
		t.Fatal(err)
	}
	wire.Save(&w, &wire.Type{Name: name, Fields: []string{field}})
	root := wire.Struct{TypeID: 1}
	root.Alloc(1)
	*root.Field(0) = value
	wire.Save(&w, wire.Uint(1))
	wire.Save(&w, &root)
	for i, ref := range referenced {
		wire.Save(&w, wire.Uint(i+2))
		wire.Save(&w, ref)
	}
	return buf.Bytes()
}

func checkpointRaw(t *testing.T, data []byte) []byte {
	t.Helper()
	var buf bytes.Buffer
	if err := state.WriteHeader(&wire.Writer{Writer: &buf}, uint64(len(data)), false); err != nil {
		t.Fatal(err)
	}
	buf.Write(data)
	return buf.Bytes()
}

// The generic printer can show an inline MemoryFile if its protobuf is given
// an ordinary raw frame in place of the historical little-endian byte count.
func checkpointMemoryFile(t *testing.T, pages int) (inline, framed []byte) {
	t.Helper()
	meta := &pgallocpb.MemoryFileMetadataProto{Version: 1}
	if pages != 0 {
		meta.MemAcct = []*pgallocpb.MemAcctRangeProto{{End: uint64(pages), KnownCommitted: true}}
	}
	data, err := proto.Marshal(meta)
	if err != nil {
		t.Fatal(err)
	}
	inline = append(binary.LittleEndian.AppendUint64(nil, uint64(len(data))), data...)
	framed = checkpointRaw(t, data)
	if pages != 0 {
		raw := checkpointRaw(t, make([]byte, pages))
		inline, framed = append(inline, raw...), append(framed, raw...)
	}
	return inline, framed
}

func dumpTestStatefile(t *testing.T, cmd Statefile, payload []byte) (string, error) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "checkpoint")
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	w, err := statefile.NewWriter(f, nil, map[string]string{"runsc_version": "test", "container_count": "1"})
	if err != nil {
		f.Close()
		t.Fatal(err)
	}
	if _, err := w.Write(payload); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	r, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	var out strings.Builder
	err = cmd.writeTo(&out, r)
	return out.String(), err
}

func TestStatefileDumpMemoryFiles(t *testing.T) {
	join := func(parts ...[]byte) []byte { return bytes.Join(parts, nil) }
	cpu := checkpointObject(t, "pkg/cpuid.FeatureSet", "marker", wire.Uint(1))
	kernel := checkpointObject(t, "pkg/sentry/kernel.Kernel", "marker", wire.Uint(2))
	roots := join(cpu, kernel)
	const ownerType = "pkg/sentry/kernel.privateMemoryFileMetadata"
	noOwners := checkpointObject(t, ownerType, "owners", wire.Nil{})
	oneOwner := checkpointObject(t, ownerType, "owners", &wire.Slice{
		Length: 1, Capacity: 2, Ref: wire.Ref{Root: 2},
	}, &wire.Array{Contents: []wire.Object{wire.Nil{}, wire.Nil{}}})
	app, appFramed := checkpointMemoryFile(t, 17)
	private, privateFramed := checkpointMemoryFile(t, 29)
	tail := checkpointObject(t, "legacyMemoryFile", "marker", wire.Uint(3))
	for _, tc := range []struct {
		name             string
		payload, generic []byte
	}{
		{"Generic", tail, tail},
		{"CPUWithoutKernel", join(cpu, tail), join(cpu, tail)},
		{"Split", roots, roots},
		{"Legacy", join(roots, tail), join(roots, tail)},
		{"ZeroOwners", join(roots, app, noOwners), join(roots, appFramed, noOwners)},
		{"Inline", join(roots, app, oneOwner, private), join(roots, appFramed, oneOwner, privateFramed)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			for _, html := range []bool{false, true} {
				got, err := dumpTestStatefile(t, Statefile{html: html}, tc.payload)
				if err != nil {
					t.Fatal(err)
				}
				print := pretty.PrintText
				var want strings.Builder
				if html {
					want.WriteString("<html><body>\n")
					print = pretty.PrintHTML
				}
				if err := print(&want, bytes.NewReader(tc.generic)); err != nil {
					t.Fatal(err)
				}
				if html {
					want.WriteString("</body></html>\n")
				}
				if tc.name == "Inline" {
					_, rest, found := strings.Cut(got, "(17 bytes non-object data)\n")
					if !found || !strings.Contains(rest, "(29 bytes non-object data)\n") {
						t.Fatalf("missing application or private page region in %q", got)
					}
				}
				if got != want.String() {
					t.Errorf("html=%t: got %q, want %q", html, got, want.String())
				}
			}
		})
	}
}
