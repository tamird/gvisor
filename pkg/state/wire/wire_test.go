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
	"fmt"
	"io"
	"reflect"
	"testing"
	"testing/iotest"
)

// emptyFirstReader exercises the permitted transient (0, nil) Reader result.
type emptyFirstReader struct {
	io.Reader
	empty bool
}

func (r *emptyFirstReader) Read(p []byte) (int, error) {
	if !r.empty {
		r.empty = true
		return 0, nil
	}
	return r.Reader.Read(p)
}

func readUintForTest(t *testing.T, r *Reader) (value uint64, err error) {
	t.Helper()
	defer func() {
		if recovered := recover(); recovered != nil {
			var ok bool
			err, ok = recovered.(error)
			if !ok {
				t.Fatalf("LoadUint panicked with %T (%v), want an error", recovered, recovered)
			}
		}
	}()
	return LoadUint(r), nil
}

func TestLoadUintReaders(t *testing.T) {
	for _, input := range []struct {
		name string
		new  func([]byte) io.Reader
	}{
		{"byte_reader", func(b []byte) io.Reader { return bytes.NewReader(b) }},
		{"reader", func(b []byte) io.Reader { return struct{ io.Reader }{bytes.NewReader(b)} }},
		{"empty_first", func(b []byte) io.Reader { return &emptyFirstReader{Reader: bytes.NewReader(b)} }},
		{"data_and_error", func(b []byte) io.Reader { return iotest.DataErrReader(bytes.NewReader(b)) }},
	} {
		t.Run(input.name, func(t *testing.T) {
			for _, test := range []struct {
				name    string
				encoded []byte
				want    uint64
				err     string
				tail    bool
			}{
				{"zero", []byte{0}, 0, "", true},
				{"one_byte", []byte{127}, 127, "", true},
				{"two_bytes", []byte{128, 1}, 128, "", true},
				{"maximum", append(bytes.Repeat([]byte{255}, 9), 1), ^uint64(0), "", true},
				{"overlong_zero", []byte{128, 0}, 0, "", true},
				{"empty", nil, 0, io.EOF.Error(), false},
				{"truncated", []byte{128}, 0, io.ErrUnexpectedEOF.Error(), false},
				{"overflow_value", append(bytes.Repeat([]byte{128}, 9), 2), 0, "binary: varint overflows a 64-bit integer", true},
				{"overflow_length", bytes.Repeat([]byte{128}, 10), 0, "binary: varint overflows a 64-bit integer", true},
			} {
				t.Run(test.name, func(t *testing.T) {
					encoded := bytes.Clone(test.encoded)
					if test.tail {
						encoded = append(encoded, 42)
					}
					r := NewReader(input.new(encoded))
					value, err := readUintForTest(t, &r)
					if test.err == "" {
						if err != nil {
							t.Fatal(err)
						}
						if got, want := value, test.want; got != want {
							t.Errorf("LoadUint = %d, want %d", got, want)
						}
					} else if got, want := fmt.Sprint(err), test.err; got != want {
						t.Errorf("LoadUint error = %q, want %q", got, want)
					}
					if test.tail {
						var tail [1]byte
						if _, err := io.ReadFull(&r, tail[:]); err != nil {
							t.Fatal(err)
						}
						if got, want := tail[0], byte(42); got != want {
							t.Errorf("following byte = %d, want %d", got, want)
						}
					}
				})
			}
		})
	}
}

func TestLoadUintReaderError(t *testing.T) {
	r := NewReader(iotest.ErrReader(io.ErrClosedPipe))
	_, err := readUintForTest(t, &r)
	if got, want := err, io.ErrClosedPipe; got != want {
		t.Errorf("LoadUint error = %v, want %v", got, want)
	}
}

// The Object API exposes these nil/empty distinctions even though they do not
// affect state restoration. Its adapter must preserve them when the parser's
// internal storage changes.
func TestLoadObjectForms(t *testing.T) {
	for _, test := range []struct {
		name  string
		value Object
		want  Object
	}{
		{"array", &Array{}, &Array{}},
		{"map", &Map{}, &Map{}},
		{"reference", &Ref{Root: ^Uint(0)}, &Ref{Root: ^Uint(0), Dots: []Dot{}}},
		{"slice", &Slice{Length: 3, Capacity: 7}, &Slice{Length: 3, Capacity: 7, Ref: Ref{Dots: []Dot{}}}},
		{"type", &Type{Name: "empty"}, &Type{Name: "empty", Fields: []string{}}},
		{"multiple", &multipleObjects{}, &multipleObjects{}},
		{"integer", Int(-1 << 63), Int(-1 << 63)},
		{"unsigned", ^Uint(0), ^Uint(0)},
	} {
		t.Run(test.name, func(t *testing.T) {
			var buf bytes.Buffer
			w := Writer{Writer: &buf}
			Save(&w, test.value)
			Save(&w, Uint(29))
			r := NewReader(&buf)
			if got, want := Load(&r), test.want; !reflect.DeepEqual(got, want) {
				t.Fatalf("Load = %#v, want %#v", got, want)
			}
			if got, want := Load(&r), Object(Uint(29)); got != want {
				t.Fatalf("following Load = %v, want %v", got, want)
			}
		})
	}
}

func TestLoadUnknownWideTag(t *testing.T) {
	var buf bytes.Buffer
	w := Writer{Writer: &buf}
	tag := Uint(1<<63) | typeInt
	tag.save(&w)
	Int(17).save(&w)
	defer func() {
		if got, want := fmt.Sprint(recover()), fmt.Sprintf("unknown header: %d", tag); got != want {
			t.Errorf("Load panic = %q, want %q", got, want)
		}
	}()
	r := NewReader(&buf)
	Load(&r)
}

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
