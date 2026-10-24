// Copyright 2018 The gVisor Authors.
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

package tests

import (
	"bufio"
	"bytes"
	"context"
	"encoding/gob"
	"fmt"
	"io"
	"math"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
)

// buildPtrObject builds a benchmark object.
func buildPtrObject(n int) any {
	b := new(benchStruct)
	for i := 0; i < n; i++ {
		b = &benchStruct{B: b}
	}
	return b
}

// buildMapObject builds a benchmark object.
func buildMapObject(n int) any {
	b := new(benchStruct)
	m := make(map[int]*benchStruct)
	for i := 0; i < n; i++ {
		m[i] = b
	}
	return &m
}

// buildSliceObject builds a benchmark object.
func buildSliceObject(n int) any {
	b := new(benchStruct)
	s := make([]*benchStruct, 0, n)
	for i := 0; i < n; i++ {
		s = append(s, b)
	}
	return &s
}

// buildFieldsObject builds a benchmark object.
func buildFieldsObject(n int) any {
	s := make([]*benchFields, 0, n)
	for i := 0; i < n; i++ {
		f := &benchFields{
			U32: uint32(i),
			U64: uint64(i) << 32,
			I:   -i,
			S:   "benchmark",
			B:   i%2 == 0,
		}
		for j := range f.Bytes {
			f.Bytes[j] = byte(i + j)
		}
		s = append(s, f)
	}
	return &s
}

// buildByteSliceObject exercises the bulk backing-array decoder.
func buildByteSliceObject(n int) any {
	b := make([]byte, n*64)
	for i := range b {
		b[i] = byte(i)
	}
	return &b
}

func buildArrayFieldsObject(n int) any {
	if n != 1024 {
		panic("fixed array benchmark requires 1024 elements")
	}
	a := new(benchArrayFields)
	for i := range a.Values {
		if i%4 != 0 {
			a.Values[i] = uint64(i)
		}
	}
	return a
}

func initializedArrayFields(n int) any {
	a := new(benchArrayFields)
	for i := range a.Values {
		a.Values[i] = 7
	}
	return a
}

func floatArrayFields(mixed bool, initial bool) any {
	a := new(benchFloatArrayFields)
	for i := range a.Values {
		a.Values[i] = math.Copysign(0, -1)
		if initial {
			a.Values[i] = 7
		} else if mixed && i%4 != 0 {
			a.Values[i] = float64(i)
		}
	}
	return a
}

func nestedArrayFields(mixed bool, initial bool) any {
	a := new(benchNestedArrayFields)
	for i := range a.Values {
		a.Values[i] = [2]float64{math.Copysign(0, -1), 0}
		if initial {
			a.Values[i] = [2]float64{7, 7}
		} else if mixed && i%4 != 0 {
			a.Values[i][1] = float64(i)
		}
	}
	return a
}

func mapArrayFields(mixed bool) any {
	a := &benchMapArrayFields{Values: make(map[int][2]float64, 512)}
	for i := 0; i < 512; i++ {
		v := [2]float64{math.Copysign(0, -1), 0}
		if mixed && i%4 != 0 {
			v[1] = float64(i)
		}
		a.Values[i] = v
	}
	return a
}

var allObjects = map[string]struct {
	New     func(int) any
	Initial func(int) any
}{
	"ptr": {
		New: buildPtrObject,
	},
	"map": {
		New: buildMapObject,
	},
	"slice": {
		New: buildSliceObject,
	},
	"fields": {
		New: buildFieldsObject,
	},
	"bytes": {
		New: buildByteSliceObject,
	},
	"arrays": {
		New:     buildArrayFieldsObject,
		Initial: initializedArrayFields,
	},
	"arrays_zero": {
		New:     func(int) any { return new(benchArrayFields) },
		Initial: initializedArrayFields,
	},
	"floats": {
		New:     func(int) any { return floatArrayFields(true, false) },
		Initial: func(int) any { return floatArrayFields(false, true) },
	},
	"floats_zero": {
		New:     func(int) any { return floatArrayFields(false, false) },
		Initial: func(int) any { return floatArrayFields(false, true) },
	},
	"nested": {
		New:     func(int) any { return nestedArrayFields(true, false) },
		Initial: func(int) any { return nestedArrayFields(false, true) },
	},
	"nested_zero": {
		New:     func(int) any { return nestedArrayFields(false, false) },
		Initial: func(int) any { return nestedArrayFields(false, true) },
	},
	"array_maps": {
		New: func(int) any { return mapArrayFields(true) },
	},
	"array_maps_zero": {
		New: func(int) any { return mapArrayFields(false) },
	},
}

// gobSave is a version of save using gob (no stats available).
func gobSave(_ context.Context, w io.Writer, v any) (_ state.Stats, err error) {
	enc := gob.NewEncoder(w)
	err = enc.Encode(v)
	return
}

// gobLoad is a version of load using gob (no stats available).
func gobLoad(_ context.Context, r io.Reader, v any) (_ state.Stats, err error) {
	dec := gob.NewDecoder(r)
	err = dec.Decode(v)
	return
}

// reflectSave is state.Save without Codec methods.
func reflectSave(ctx context.Context, w io.Writer, v any) (state.Stats, error) {
	defer state.DisableCodecs()()
	return state.Save(ctx, w, v)
}

// reflectLoad is state.Load without Codec methods.
func reflectLoad(ctx context.Context, r io.Reader, v any) (state.Stats, error) {
	defer state.DisableCodecs()()
	return state.Load(ctx, r, v)
}

var allAlgos = map[string]struct {
	Save   func(context.Context, io.Writer, any) (state.Stats, error)
	Load   func(context.Context, io.Reader, any) (state.Stats, error)
	MaxPtr int
}{
	"state": {
		Save: state.Save,
		Load: state.Load,
	},
	"state-reflect": {
		Save: reflectSave,
		Load: reflectLoad,
	},
	"gob": {
		Save: gobSave,
		Load: gobLoad,
	},
}

func BenchmarkEncoding(b *testing.B) {
	for objName, objInfo := range allObjects {
		for algoName, algoInfo := range allAlgos {
			b.Run(fmt.Sprintf("%s/%s", objName, algoName), func(b *testing.B) {
				v := objInfo.New(1024)
				b.ReportAllocs()
				for b.Loop() {
					if _, err := algoInfo.Save(context.Background(), io.Discard, v); err != nil {
						b.Errorf("save failed: %v", err)
					}
				}
			})
		}
	}
}

func BenchmarkDecoding(b *testing.B) {
	for objName, objInfo := range allObjects {
		for algoName, algoInfo := range allAlgos {
			b.Run(fmt.Sprintf("%s/%s", objName, algoName), func(b *testing.B) {
				v := objInfo.New(1024)
				buf := new(bytes.Buffer)
				if _, err := algoInfo.Save(context.Background(), buf, v); err != nil {
					b.Errorf("save failed: %v", err)
				}
				if objInfo.Initial != nil {
					v = objInfo.Initial(1024)
				}
				b.ReportAllocs()
				var r bytes.Reader
				// Statefiles are read through a bufio.Reader.
				br := bufio.NewReaderSize(&r, 256<<10)
				for b.Loop() {
					r.Reset(buf.Bytes())
					br.Reset(&r)
					if _, err := algoInfo.Load(context.Background(), br, v); err != nil {
						b.Errorf("load failed: %v", err)
					}
				}
			})
		}
	}
}
