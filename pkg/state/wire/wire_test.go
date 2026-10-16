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
			Load(&Reader{Reader: &buf})
		})
	}
}

// The count must not narrow to an empty collection before allocation checks it.
func TestLoadOversizedCount(t *testing.T) {
	for _, test := range []struct {
		name string
		tag  Uint
	}{
		{"array", typeArray},
		{"map", typeMap},
		{"fields", typeMultipleObjects},
		{"path", typeRef},
		{"schema", typeType},
	} {
		t.Run(test.name, func(t *testing.T) {
			for _, count := range []Uint{1 << 63, ^Uint(0)} {
				t.Run(fmt.Sprint(count), func(t *testing.T) {
					var buf bytes.Buffer
					writer := Writer{Writer: &buf}
					test.tag.save(&writer)
					switch test.tag {
					case typeRef:
						Uint(1).save(&writer)
					case typeType:
						name := String("schema")
						name.save(&writer)
					}
					count.save(&writer)
					defer func() {
						got := recover()
						if got == nil || got == io.EOF {
							t.Errorf("Load panic = %v, want count rejection before reading elements", got)
						}
					}()
					Load(&Reader{Reader: &buf})
				})
			}
		})
	}
}

func TestEmptyCollections(t *testing.T) {
	for _, test := range []struct {
		name    string
		tag     Uint
		wantNil bool
	}{
		{"array", typeArray, true},
		{"map", typeMap, true},
		{"fields", typeMultipleObjects, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			var buf bytes.Buffer
			writer := Writer{Writer: &buf}
			test.tag.save(&writer)
			Uint(0).save(&writer)
			value := Load(&Reader{Reader: &buf})
			if got, want := value.Kind(), Kind(test.tag); got != want {
				t.Fatalf("kind = %d, want %d", got, want)
			}
			if test.tag == typeMap {
				if got, want := value.Map() == nil, test.wantNil; got != want {
					t.Errorf("nil entries = %t, want %t", got, want)
				}
				return
			}
			values := value.Array()
			if got, want := len(values), 0; got != want {
				t.Errorf("length = %d, want %d", got, want)
			}
			if got, want := values == nil, test.wantNil; got != want {
				t.Errorf("nil elements = %t, want %t", got, want)
			}
		})
	}
}

func TestValueOwnership(t *testing.T) {
	field := FieldName("Value")
	want := &Array{Contents: []Object{
		&Ref{Root: 7, Dots: []Dot{Index(257), &field}, Type: &TypeSpecArray{Count: 8, Type: TypeID(3)}},
		&Ref{Root: 9, Dots: []Dot{Index(0)}, Type: &TypeSpecPointer{Type: TypeID(4)}},
	}}
	// Retain a nested array with independent paths and type specifications.
	// Following records must not alter any of its children.
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
	parent.Alloc(4)
	text := String("independent string")
	*parent.Field(0), *parent.Field(1), *parent.Field(2) = Uint(17), want, &text
	*parent.Field(3) = &Map{
		Keys:   []Object{Int(29), Int(17)},
		Values: want.Contents[:2],
	}
	var buf bytes.Buffer
	w := Writer{Writer: &buf}
	Save(&w, &parent)
	Save(&w, &Array{Contents: []Object{&text, &text, &text}})
	Save(&w, reuse)
	// Full-width reference metadata and more than 255 path elements must
	// survive both field-name and index paths in successive records.
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
	encoded := buf.Bytes()
	r := Reader{Reader: &buf}
	first := Load(&r)
	retained := first.Field(1).Array()
	retainedText := first.Field(2).Text()
	retainedMap := first.Field(3).Map()
	// Keep only a nested subtree and text while reading unrelated records.
	// Their lifetime must not depend on the reader or its input storage.
	Load(&r)
	// A large scalar array follows reference metadata and a short record.
	reused := Load(&r).Array()
	if got, want := len(reused), len(reuse.Contents); got != want {
		t.Fatalf("reused length = %d, want %d", got, want)
	}
	for i := range reuse.Contents {
		if got, want := reused[i].Uint(), uint64(i&1); got != want {
			t.Fatalf("reused element %d = %d, want %d", i, got, want)
		}
	}
	fields := Load(&r).Reference()
	if got, want := len(fields.Dots), len(path.Dots); got != want {
		t.Fatalf("path length = %d, want %d", got, want)
	}
	for i := range path.Dots {
		if got, want := fields.Dots[i], &field; !reflect.DeepEqual(got, want) {
			t.Fatalf("field step %d = %+v, want %+v", i, got, want)
		}
	}
	if got, want := Load(&r).Reference(), *path; !reflect.DeepEqual(got, want) {
		t.Fatalf("reused index path = %#v, want %#v", got, want)
	}
	if got, want := Load(&r).Uint(), uint64(29); got != want {
		t.Fatalf("following value = %d, want %d", got, want)
	}
	clear(encoded)
	if got, want := len(retained), len(want.Contents); got != want {
		t.Fatalf("owned subtree length = %d, want %d", got, want)
	}
	for i, expected := range want.Contents {
		if got, want := retained[i].Reference(), *expected.(*Ref); !reflect.DeepEqual(got, want) {
			t.Errorf("owned reference %d = %#v, want %#v", i, got, want)
		}
	}
	if got, want := retainedText, string(text); got != want {
		t.Errorf("retained text = %q, want %q", got, want)
	}
	if got, want := len(retainedMap), 2; got != want {
		t.Fatalf("retained map length = %d, want %d", got, want)
	}
	for i, key := range []int64{29, 17} {
		if got, want := retainedMap[i].Key.Int(), key; got != want {
			t.Errorf("retained map key %d = %d, want %d", i, got, want)
		}
		if got, want := retainedMap[i].Value.Reference(), *want.Contents[i].(*Ref); !reflect.DeepEqual(got, want) {
			t.Errorf("retained map value %d = %#v, want %#v", i, got, want)
		}
	}
}

func TestSchemaOwnership(t *testing.T) {
	schemas := []*Type{
		{Name: "first", Fields: []string{"left", "right"}},
		{Name: "empty", Fields: []string{}},
		{Name: "last", Fields: []string{"value"}},
	}
	var saved bytes.Buffer
	writer := Writer{Writer: &saved}
	array := &Array{}
	for _, schema := range schemas {
		array.Contents = append(array.Contents, schema)
	}
	Save(&writer, array)
	parsed := Load(&Reader{Reader: &saved}).Array()
	retained := parsed[0].Type()

	// A later record may parse a complete nested schema before failing.
	// It must not return that partial record or overwrite retained metadata.
	Save(&writer, array)
	data := saved.Bytes()
	func() {
		defer func() {
			if got, want := recover(), io.EOF; got != want {
				t.Errorf("truncated schema panic = %v, want %v", got, want)
			}
		}()
		Load(&Reader{Reader: bytes.NewReader(data[:len(data)-1])})
	}()
	saved.Reset()
	Save(&writer, &Type{Name: "replacement", Fields: []string{"other"}})
	Load(&Reader{Reader: &saved})
	if got, want := retained, schemas[0]; !reflect.DeepEqual(got, want) {
		t.Errorf("retained schema = %+v, want %+v", got, want)
	}
	for i, schema := range schemas {
		if got, want := parsed[i].Type(), schema; !reflect.DeepEqual(got, want) {
			t.Errorf("owned schema %d = %+v, want %+v", i, got, want)
		}
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
