// Copyright 2024 The gVisor Authors.
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

package wire

import (
	"bufio"
	"bytes"
	"io"
	"slices"
	"testing"
)

// BenchmarkUintSave benchmarks saving a Uint. This benchmark is important
// because almost all types in this package boil down to Uints. So
// Uint.save() performance is critical for checkpoint performance.
func BenchmarkUintSave(b *testing.B) {
	w := Writer{Writer: bufio.NewWriter(io.Discard)}
	n := Uint(0xdeadbeef)
	for i := 0; i < b.N; i++ {
		n.save(&w)
	}
}

// These literal streams bind the first-element tag rule independently of the
// reader and materializer. Later elements contain bodies without repeated tags.
func TestHomogeneousViews(t *testing.T) {
	for _, test := range []struct {
		name  string
		data  []byte
		check func(*testing.T, Value)
	}{
		{
			name: "array",
			data: []byte{9, 3, 1, 34, 1, 128, 2},
			check: func(t *testing.T, value Value) {
				length, elements := value.Array()
				if got, want := length, 3; got != want {
					t.Fatalf("array length = %d, want %d", got, want)
				}
				for _, want := range []Int{17, -1, 128} {
					item, ok := elements.Next()
					if !ok {
						t.Fatal("missing array element")
					}
					if got := item.Int(); got != want {
						t.Errorf("array element = %d, want %d", got, want)
					}
				}
				if _, ok := elements.Next(); ok {
					t.Fatal("unexpected array element")
				}
			},
		},
		{
			name: "map",
			data: []byte{10, 2, 1, 2, 7, 1, 'a', 4, 1, 'b'},
			check: func(t *testing.T, value Value) {
				entries := value.Map()
				for i, want := range []string{"a", "b"} {
					key, element, ok := entries.NextPair()
					if !ok {
						t.Fatal("missing map entry")
					}
					if got, want := key.Int(), Int(i+1); got != want {
						t.Errorf("map key = %d, want %d", got, want)
					}
					if got := element.Text(); got != want {
						t.Errorf("map value = %q, want %q", got, want)
					}
				}
				if _, _, ok := entries.NextPair(); ok {
					t.Fatal("unexpected map entry")
				}
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			input := bytes.NewReader(append(slices.Clone(test.data), 0xff))
			value := ReadValue(&Reader{Reader: input})
			test.check(t, value)
			if got, want := input.Len(), 1; got != want {
				t.Errorf("trailing bytes = %d, want %d", got, want)
			}
			var materialized bytes.Buffer
			Save(&Writer{Writer: &materialized}, value.Object())
			if got, want := materialized.Bytes(), test.data; !bytes.Equal(got, want) {
				t.Errorf("materialized encoding = %v, want %v", got, want)
			}
		})
	}
}

func TestSingletonFieldView(t *testing.T) {
	// The existing singleton representation ignores the field index. Schema
	// reconciliation is a separate check and must not silently tighten this.
	value := ReadValue(&Reader{Reader: bytes.NewReader([]byte{11, 1, 1, 34})})
	fields := value.Struct()
	if got, want := fields.Len(), 1; got != want {
		t.Fatalf("field count = %d, want %d", got, want)
	}
	for _, slot := range []int{0, 2, -1, 0} {
		if got, want := fields.Field(slot).Int(), Int(17); got != want {
			t.Errorf("slot %d = %d, want %d", slot, got, want)
		}
	}
}
