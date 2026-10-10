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

// TestFramedFieldResave preserves the mutable Field contract for public frames
// and decoded frames, including a pointer retained across multiple writes.
func TestFramedFieldResave(t *testing.T) {
	var arena FrameArena
	child := &Struct{TypeID: 2}
	child.AllocFrame(&arena, 1)
	*child.Field(0) = Int(1)
	parent := &Struct{TypeID: 1}
	parent.AllocFrame(&arena, 1)
	*parent.Field(0) = child
	arena.Finish()

	roundTrip := func(value Object) Object {
		t.Helper()
		var encoded bytes.Buffer
		Save(&Writer{Writer: &encoded}, value)
		loaded := Load(&Reader{Reader: &encoded})
		if got, want := encoded.Len(), 0; got != want {
			t.Fatalf("unread encoded bytes = %d, want %d", got, want)
		}
		return loaded
	}
	decoded := roundTrip(parent).(*Struct)
	for _, test := range []struct {
		name   string
		parent *Struct
	}{
		{name: "public", parent: parent},
		{name: "decoded", parent: decoded},
	} {
		t.Run(test.name, func(t *testing.T) {
			nested := (*test.parent.Field(0)).(*Struct)
			retained := nested.Field(0)
			for _, value := range []Int{128, 1 << 28, 1} {
				// Change the varint width without calling Field again. Both
				// the child span and containing field length must be fresh.
				*retained = value
				loaded := roundTrip(test.parent).(*Struct)
				loadedChild := (*loaded.Field(0)).(*Struct)
				if got, want := *loadedChild.Field(0), Object(value); got != want {
					t.Errorf("nested value = %v, want %v", got, want)
				}
			}
		})
	}
}

func TestFramedHomogeneousResave(t *testing.T) {
	for _, name := range []string{"array", "map"} {
		t.Run(name, func(t *testing.T) {
			var arena FrameArena
			values := make([]Object, 2)
			for i := range values {
				value := &Struct{TypeID: 1}
				value.AllocFrame(&arena, 1)
				*value.Field(0) = Int(i)
				values[i] = value
			}
			var original Object = &Array{Contents: values}
			if name == "map" {
				original = &Map{Keys: []Object{Uint(0), Uint(1)}, Values: values}
			}
			arena.Finish()
			roundTrip := func(value Object) Object {
				t.Helper()
				var encoded bytes.Buffer
				Save(&Writer{Writer: &encoded}, value)
				return Load(&Reader{Reader: &encoded})
			}
			second := func(value Object) *Struct {
				switch x := value.(type) {
				case *Array:
					return x.Contents[1].(*Struct)
				case *Map:
					return x.Values[1].(*Struct)
				default:
					t.Fatalf("unexpected container %T", value)
					return nil
				}
			}
			loaded := roundTrip(original)
			retained := second(loaded).Field(0)
			for _, value := range []Int{128, 1 << 28} {
				*retained = value
				resaved := roundTrip(loaded)
				if got, want := *second(resaved).Field(0), Object(value); got != want {
					t.Errorf("second entry = %v, want %v", got, want)
				}
			}
		})
	}
}

func TestFrameSealRefreshesSizes(t *testing.T) {
	var arena FrameArena
	child := &Struct{TypeID: 2}
	child.AllocFrame(&arena, 1)
	retained := child.Field(0)
	*retained = Int(1)
	parent := &Struct{TypeID: 1}
	parent.AllocFrame(&arena, 1)
	*parent.Field(0) = child
	arena.Finish()
	// No Field call invalidates the cached sizes after this mutation.
	*retained = Int(1 << 28)
	arena.Seal()
	var encoded bytes.Buffer
	Save(&Writer{Writer: &encoded}, parent)
	loaded := Load(&Reader{Reader: &encoded}).(*Struct)
	loadedChild := (*loaded.Field(0)).(*Struct)
	if got, want := *loadedChild.Field(0), Object(Int(1<<28)); got != want {
		t.Errorf("sealed nested value = %v, want %v", got, want)
	}
}
