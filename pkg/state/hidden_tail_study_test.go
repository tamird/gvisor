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

package state

import (
	"bytes"
	"context"
	"crypto/sha256"
	"io"
	"testing"
)

// TestGraphSliceHiddenTail compares streams that differ only at the last
// byte of an eight-byte backing array after its first view is shortened.
func TestGraphSliceHiddenTail(t *testing.T) {
	type writer func(context.Context, io.Writer, any) (Stats, error)
	check := func(t *testing.T, save writer, load bool) {
		for _, tc := range []struct {
			name string
			full bool
		}{
			{"hidden", false}, {"second_live_view", true},
		} {
			t.Run(tc.name, func(t *testing.T) {
				var wire [2][]byte
				for i, marker := range []byte{0xa7, 0xc9} {
					buf := make([]byte, 8)
					buf[0], buf[7] = 0x31, marker
					views := [2][]byte{buf[:1], nil}
					if tc.full {
						views[1] = buf[:]
					}
					var w bytes.Buffer
					if _, err := save(context.Background(), &w, &views); err != nil {
						t.Fatalf("Save: %v", err)
					}
					wire[i] = w.Bytes()
					restored := -1
					alias := false
					if load {
						var got [2][]byte
						if _, err := Load(context.Background(), bytes.NewReader(wire[i]), &got); err != nil {
							t.Fatalf("Load: %v", err)
						}
						if len(got[0]) != 1 || cap(got[0]) != 8 || got[0][0] != 0x31 {
							t.Fatalf("incorrect first view: len=%d cap=%d bytes=%x", len(got[0]), cap(got[0]), got[0])
						}
						all := got[0][:cap(got[0])]
						restored = int(all[7])
						if tc.full {
							if len(got[1]) != 8 || cap(got[1]) != 8 {
								t.Fatalf("incorrect second view: len=%d cap=%d", len(got[1]), cap(got[1]))
							}
							alias = &all[7] == &got[1][7]
							if !alias {
								t.Fatal("views do not alias after restore")
							}
						} else if got[1] != nil {
							t.Fatal("unexpected second view")
						}
					}
					t.Logf("TAIL case=%s marker=%d source_after=%d restored=%d alias=%t wire=%d sha256=%x", t.Name(), marker, buf[7], restored, alias, len(wire[i]), sha256.Sum256(wire[i]))
				}
				t.Logf("TAIL case=%s equal_streams=%t", t.Name(), bytes.Equal(wire[0], wire[1]))
			})
		}
	}
	t.Run("graph", func(t *testing.T) { check(t, Save, true) })
}
