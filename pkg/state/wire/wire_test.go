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

// TestCapturedFieldResave exercises mutable public fields, including a pointer
// retained while another save completes and a nested child's value changes.
func TestCapturedFieldResave(t *testing.T) {
	childLayout := NewCaptureLayout([]uint64{ScalarInt, ScalarString})
	parentLayout := NewCaptureLayout([]uint64{ScalarObject, ScalarUint})
	layout := func(id TypeID) *CaptureLayout {
		if id == 1 {
			return parentLayout
		}
		return childLayout
	}
	var arena CaptureArena
	child := &Struct{TypeID: 2}
	child.AllocCapture(&arena, childLayout)
	child.StoreScalar(0, Scalar{Kind: ScalarInt, Int: 1})
	child.StoreScalar(1, Scalar{Kind: ScalarString, Text: "owned"})
	parent := &Struct{TypeID: 1}
	parent.AllocCapture(&arena, parentLayout)
	*parent.Field(0) = child
	parent.StoreScalar(1, Scalar{Kind: ScalarUint, Uint: 7})
	roundTrip := func(obj Object, direct bool) Object {
		t.Helper()
		var encoded bytes.Buffer
		Save(&Writer{Writer: &encoded}, obj)
		r := Reader{Reader: &encoded}
		if direct {
			r.CaptureLayout = layout
		}
		loaded := Load(&r)
		if got, want := encoded.Len(), 0; got != want {
			t.Fatalf("unread bytes = %d, want %d", got, want)
		}
		return loaded
	}
	decoded := roundTrip(parent, true).(*Struct)
	for _, test := range []struct {
		name   string
		parent *Struct
	}{
		{"public", parent}, {"decoded", decoded},
	} {
		t.Run(test.name, func(t *testing.T) {
			nested := (*test.parent.Field(0)).(*Struct)
			retained := nested.Field(0)
			for _, value := range []Int{128, 1 << 28, 1} {
				*retained = value
				loaded := roundTrip(test.parent, false).(*Struct)
				loadedChild := (*loaded.Field(0)).(*Struct)
				if got, want := *loadedChild.Field(0), Object(value); got != want {
					t.Errorf("nested value = %v, want %v", got, want)
				}
			}
			// Typed writes must update the same stable Field override.
			nested.StoreScalar(0, Scalar{Kind: ScalarInt, Int: 42})
			if got, want := *retained, Object(Int(42)); got != want {
				t.Errorf("retained value = %v, want %v", got, want)
			}
		})
	}
}

func TestCapturedHomogeneousResave(t *testing.T) {
	layout := NewCaptureLayout([]uint64{ScalarInt, ScalarBool})
	for _, name := range []string{"array", "map"} {
		t.Run(name, func(t *testing.T) {
			var arena CaptureArena
			values := make([]Object, 2)
			for i := range values {
				value := &Struct{TypeID: 1}
				value.AllocCapture(&arena, layout)
				value.StoreScalar(0, Scalar{Kind: ScalarInt, Int: int64(i)})
				value.StoreScalar(1, Scalar{Kind: ScalarBool, Uint: 1})
				values[i] = value
			}
			var original Object = &Array{Contents: values}
			if name == "map" {
				original = &Map{Keys: []Object{Uint(0), Uint(1)}, Values: values}
			}
			roundTrip := func(value Object) Object {
				t.Helper()
				var encoded bytes.Buffer
				Save(&Writer{Writer: &encoded}, value)
				return Load(&Reader{Reader: &encoded, CaptureLayout: func(TypeID) *CaptureLayout { return layout }})
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
