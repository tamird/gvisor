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
)

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
			r := Reader{Reader: &buf}
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
	Load(&Reader{Reader: &buf})
}

func TestValueBufferOwnership(t *testing.T) {
	field := FieldName("Value")
	want := &Array{Contents: []Object{
		&Ref{Root: 7, Dots: []Dot{Index(257), &field}, Type: &TypeSpecArray{Count: 8, Type: TypeID(3)}},
		&Ref{Root: 9, Dots: []Dot{Index(0)}, Type: &TypeSpecPointer{Type: TypeID(4)}},
	}}
	parent := Struct{TypeID: 1}
	parent.Alloc(3)
	text := String("independent string")
	*parent.Field(0), *parent.Field(1), *parent.Field(2) = Uint(17), want, &text
	var buf bytes.Buffer
	w := Writer{Writer: &buf}
	Save(&w, &parent)
	Save(&w, &Array{Contents: []Object{&text, &text, &text}})
	Save(&w, Uint(29))
	r := Reader{Reader: &buf}
	var values ValueBuffer
	first := values.Load(&r)
	owned := first.Field(1).Own()
	retainedText := first.Field(2).Text()
	// Reuse both node and string storage, then release the entire parser.
	// Only the selected subtree and independently owned string remain usable.
	values.Load(&r)
	if got, want := values.Load(&r).Uint(), uint64(29); got != want {
		t.Fatalf("following value = %d, want %d", got, want)
	}
	values.Release()
	if got, want := materialize(owned), Object(want); !reflect.DeepEqual(got, want) {
		t.Errorf("owned subtree = %#v, want %#v", got, want)
	}
	if got, want := retainedText, string(text); got != want {
		t.Errorf("retained text = %q, want %q", got, want)
	}
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
