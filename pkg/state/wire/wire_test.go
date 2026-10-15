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

func TestLoadUnknownWideTag(t *testing.T) {
	for _, test := range []struct {
		name string
		tag  Uint
	}{
		{"value", Uint(1<<63) | typeInt},
		{"type", Uint(1<<63) | typeSpecNil},
	} {
		t.Run(test.name, func(t *testing.T) {
			var buf bytes.Buffer
			w := Writer{Writer: &buf}
			if test.name == "type" {
				typeInterface.save(&w)
			}
			test.tag.save(&w)
			Save(&w, Int(17))
			defer func() {
				if got, want := fmt.Sprint(recover()), fmt.Sprintf("unknown header: %d", test.tag); got != want {
					t.Errorf("Load panic = %q, want %q", got, want)
				}
			}()
			var values ValueBuffer
			defer values.Release()
			values.Load(&Reader{Reader: &buf})
		})
	}
}

func TestValueBufferOwnership(t *testing.T) {
	field := FieldName("Value")
	want := &Array{Contents: []Object{
		&Ref{Root: 7, Dots: []Dot{Index(257), &field}, Type: &TypeSpecArray{Count: 8, Type: TypeID(3)}},
		&Ref{Root: 9, Dots: []Dot{Index(0)}, Type: &TypeSpecPointer{Type: TypeID(4)}},
	}}
	// Many recursive children force storage growth after reserving the array's
	// element range. Their logical indices must survive every growth boundary.
	for i := range 257 {
		want.Contents = append(want.Contents, &Ref{
			Root: Uint(i + 100),
			Dots: []Dot{Index(i * 3), &field},
			Type: &TypeSpecPointer{Type: TypeID(4)},
		})
	}
	reuse := &Array{Contents: make([]Object, 257)}
	for i := range reuse.Contents {
		reuse.Contents[i] = Uint(i & 1)
	}
	parent := Struct{TypeID: 1}
	parent.Alloc(3)
	text := String("independent string")
	*parent.Field(0), *parent.Field(1), *parent.Field(2) = Uint(17), want, &text
	var buf bytes.Buffer
	w := Writer{Writer: &buf}
	Save(&w, &parent)
	Save(&w, &Array{Contents: []Object{&text, &text, &text}})
	Save(&w, reuse)
	// A path count is an operand, not a byte tag. Reusing the same slots for
	// index steps must also replace the previous field-name tags with zero.
	path := &Ref{
		Root: ^Uint(0),
		Dots: make([]Dot, 257),
		Type: &TypeSpecArray{Count: ^Uint(0), Type: TypeID(1<<63 | 3)},
	}
	for i := range path.Dots {
		path.Dots[i] = &field
	}
	Save(&w, path)
	for i := range path.Dots {
		path.Dots[i] = Index(i)
	}
	Save(&w, path)
	Save(&w, Uint(29))
	r := Reader{Reader: &buf}
	var values ValueBuffer
	first := values.Load(&r)
	owned := first.Field(1).Own()
	retainedText := first.Field(2).Text()
	// Reuse both node and string storage, then release the entire parser.
	// Only the selected subtree and independently owned string remain usable.
	values.Load(&r)
	// Grow the logical record again after a short one, reusing storage that
	// previously held reference metadata for ordinary scalar values.
	reused := values.Load(&r)
	if got, want := reused.Len(), len(reuse.Contents); got != want {
		t.Fatalf("reused length = %d, want %d", got, want)
	}
	for i := range reuse.Contents {
		if got, want := reused.Element(i).Uint(), uint64(i&1); got != want {
			t.Fatalf("reused element %d = %d, want %d", i, got, want)
		}
	}
	fields := values.Load(&r).ReferenceView()
	if got, want := fields.Dots(), len(path.Dots); got != want {
		t.Fatalf("path length = %d, want %d", got, want)
	}
	for i := range path.Dots {
		if got, want := fields.Dot(i), (PathElement{Name: string(field), Field: true}); got != want {
			t.Fatalf("field step %d = %+v, want %+v", i, got, want)
		}
	}
	if got, want := values.Load(&r).Reference(), *path; !reflect.DeepEqual(got, want) {
		t.Fatalf("reused index path = %#v, want %#v", got, want)
	}
	if got, want := values.Load(&r).Uint(), uint64(29); got != want {
		t.Fatalf("following value = %d, want %d", got, want)
	}
	values.Release()
	if got, want := owned.Len(), len(want.Contents); got != want {
		t.Fatalf("owned subtree length = %d, want %d", got, want)
	}
	for i, expected := range want.Contents {
		if got, want := owned.Element(i).Reference(), *expected.(*Ref); !reflect.DeepEqual(got, want) {
			t.Errorf("owned reference %d = %#v, want %#v", i, got, want)
		}
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
