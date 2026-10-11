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

package wire

import (
	"bytes"
	"slices"
	"testing"
)

func TestRefArrayWire(t *testing.T) {
	field := FieldName("field")
	for _, test := range []struct {
		name string
		refs []Ref
	}{
		{"empty", nil},
		{"nil", []Ref{{}}},
		{"mixed", []Ref{{}, {Root: 127}, {Root: 129, Dots: []Dot{Index(128), &field}, Type: TypeID(3)}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			compact := &RefArray{Contents: slices.Clone(test.refs)}
			ordinary := &Array{Contents: make([]Object, len(test.refs))}
			for i, ref := range test.refs {
				ordinary.Contents[i] = &ref
			}
			check := func() {
				t.Helper()
				var got, want bytes.Buffer
				Save(&Writer{Writer: &got}, compact)
				Save(&Writer{Writer: &want}, ordinary)
				if !bytes.Equal(got.Bytes(), want.Bytes()) {
					t.Fatalf("reference array = %x, want ordinary array %x", got.Bytes(), want.Bytes())
				}
				loaded := Load(&Reader{Reader: bytes.NewReader(got.Bytes())})
				if _, ok := loaded.(*Array); !ok {
					t.Fatalf("loaded type = %T, want *Array", loaded)
				}
				got.Reset()
				Save(&Writer{Writer: &got}, loaded)
				if !bytes.Equal(got.Bytes(), want.Bytes()) {
					t.Fatalf("loaded re-encoding = %x, want %x", got.Bytes(), want.Bytes())
				}
			}
			check()
			if len(test.refs) > 1 {
				// A retained resolver handle can change a varint's width after
				// a previous save. There is no cached representation to refresh.
				handle := &compact.Contents[1]
				handle.Root = 128
				ordinary.Contents[1].(*Ref).Root = 128
				check()
			}
		})
	}
}

func TestNestedRefArrays(t *testing.T) {
	compact := &Array{Contents: []Object{
		&RefArray{Contents: []Ref{{}}},
		&RefArray{Contents: []Ref{{Root: 128}, {Root: 5}}},
	}}
	ordinary := &Array{Contents: []Object{
		&Array{Contents: []Object{&Ref{}}},
		&Array{Contents: []Object{&Ref{Root: 128}, &Ref{Root: 5}}},
	}}
	var got, want bytes.Buffer
	Save(&Writer{Writer: &got}, compact)
	Save(&Writer{Writer: &want}, ordinary)
	if !bytes.Equal(got.Bytes(), want.Bytes()) {
		t.Fatalf("nested reference arrays = %x, want %x", got.Bytes(), want.Bytes())
	}
	loaded := Load(&Reader{Reader: bytes.NewReader(got.Bytes())}).(*Array)
	loaded.Contents[1].(*Array).Contents[0].(*Ref).Root = 16384
	ordinary.Contents[1].(*Array).Contents[0].(*Ref).Root = 16384
	got.Reset()
	want.Reset()
	Save(&Writer{Writer: &got}, loaded)
	Save(&Writer{Writer: &want}, ordinary)
	if !bytes.Equal(got.Bytes(), want.Bytes()) {
		t.Fatalf("mutated nested arrays = %x, want %x", got.Bytes(), want.Bytes())
	}
}
