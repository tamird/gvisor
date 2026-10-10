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
	"math"
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

// Compare the compact representation with the unchanged public Array emitter.
// This pins full-value zeros, NaN/sign bits and nested homogeneous dispatch.
func TestPrimitiveArrayWireCompatibility(t *testing.T) {
	nan := math.Float64frombits(0x7ff8000000000042)
	negativeZero := math.Copysign(0, -1)
	text := String("text")
	empty := String("")
	c64 := Complex64(complex(negativeZero, -3))
	c128 := Complex128(complex(nan, negativeZero))
	for _, test := range []struct {
		name string
		got  Object
		want *Array
	}{
		{"empty", CaptureUintArray([]uint64{}), &Array{}},
		{"bool", CaptureBoolArray([]bool{false, true}), &Array{Contents: []Object{Bool(false), Bool(true)}}},
		{"signed", CaptureIntArray([]int64{-1 << 63, 0, 1<<63 - 1}), &Array{Contents: []Object{Int(-1 << 63), Int(0), Int(1<<63 - 1)}}},
		{"unsigned", CaptureUintArray([]uint64{0, ^uint64(0)}), &Array{Contents: []Object{Uint(0), Uint(^uint64(0))}}},
		{"float32", CaptureFloat32Array([]float32{float32(negativeZero), float32(nan)}), &Array{Contents: []Object{Float32(float32(negativeZero)), Float32(float32(nan))}}},
		{"float64", CaptureFloat64Array([]float64{negativeZero, nan}), &Array{Contents: []Object{Float64(negativeZero), Float64(nan)}}},
		{"complex64", CaptureComplex64Array([]complex64{complex64(c64)}), &Array{Contents: []Object{&c64}}},
		{"complex128", CaptureComplex128Array([]complex128{complex128(c128)}), &Array{Contents: []Object{&c128}}},
		{"string", CaptureStringArray([]string{"", "text"}), &Array{Contents: []Object{&empty, &text}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			for _, nested := range []bool{false, true} {
				got, want := test.got, Object(test.want)
				if nested {
					// The second array bypasses Save's dynamic type dispatch.
					got = &Array{Contents: []Object{test.got, test.got}}
					want = &Array{Contents: []Object{test.want, test.want}}
				}
				var encoded, legacy bytes.Buffer
				Save(&Writer{Writer: &encoded}, got)
				Save(&Writer{Writer: &legacy}, want)
				if got, want := encoded.Bytes(), legacy.Bytes(); !bytes.Equal(got, want) {
					t.Fatalf("wire bytes (nested=%t) = %x, want %x", nested, got, want)
				}
				// The ordinary decoder's public Array remains a mutable value.
				decoded := Load(&Reader{Reader: bytes.NewReader(encoded.Bytes())})
				var restored bytes.Buffer
				Save(&Writer{Writer: &restored}, decoded)
				if got, want := restored.Bytes(), legacy.Bytes(); !bytes.Equal(got, want) {
					t.Errorf("resaved bytes (nested=%t) = %x, want %x", nested, got, want)
				}
			}
		})
	}
}

func TestPrimitiveArraySnapshot(t *testing.T) {
	values := []uint16{7, 11}
	captured := CaptureUintArray(values)
	values[0] = 99
	var encoded bytes.Buffer
	Save(&Writer{Writer: &encoded}, captured)
	loaded := Load(&Reader{Reader: &encoded}).(*Array)
	if got, want := loaded.Contents[0], Object(Uint(7)); got != want {
		t.Errorf("captured first element = %v, want %v", got, want)
	}
}
