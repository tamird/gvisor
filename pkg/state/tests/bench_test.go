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
	"bytes"
	"context"
	"encoding/gob"
	"fmt"
	"io"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/tcpip"
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

var allObjects = map[string]struct {
	New func(int) any
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

var allAlgos = map[string]struct {
	Save   func(context.Context, io.Writer, any) (state.Stats, error)
	Load   func(context.Context, io.Reader, any) (state.Stats, error)
	MaxPtr int
}{
	"state": {
		Save: state.Save,
		Load: state.Load,
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
				b.ReportAllocs()
				var r bytes.Reader
				for b.Loop() {
					r.Reset(buf.Bytes())
					if _, err := algoInfo.Load(context.Background(), &r, v); err != nil {
						b.Errorf("load failed: %v", err)
					}
				}
			})
		}
	}
}

// controlMessageBenchmark uses a real timestamp-bearing state type. Its
// timestamp used a UnixNano hook before the binary-codec change.
func controlMessageBenchmark(b *testing.B) (tcpip.ReceivableControlMessages, []byte) {
	b.Helper()
	message := tcpip.ReceivableControlMessages{
		HasTimestamp: true,
		Timestamp:    time.Date(2026, time.October, 7, 12, 0, 0, 123456789, time.UTC),
	}
	var buf bytes.Buffer
	if _, err := state.Save(b.Context(), &buf, &message); err != nil {
		b.Fatal(err)
	}
	var restored tcpip.ReceivableControlMessages
	if _, err := state.Load(b.Context(), bytes.NewReader(buf.Bytes()), &restored); err != nil {
		b.Fatal(err)
	}
	if !restored.HasTimestamp || !restored.Timestamp.Equal(message.Timestamp) {
		b.Fatalf("restored control message = %+v, want timestamp %v", restored, message.Timestamp)
	}
	return message, buf.Bytes()
}

func BenchmarkControlMessageEncoding(b *testing.B) {
	message, encoded := controlMessageBenchmark(b)
	b.ReportAllocs()
	for b.Loop() {
		if _, err := state.Save(b.Context(), io.Discard, &message); err != nil {
			b.Fatal(err)
		}
	}
	b.ReportMetric(float64(len(encoded)), "wire-B/op")
}

func BenchmarkControlMessageDecoding(b *testing.B) {
	_, encoded := controlMessageBenchmark(b)
	var restored tcpip.ReceivableControlMessages
	var reader bytes.Reader
	b.ReportAllocs()
	for b.Loop() {
		reader.Reset(encoded)
		if _, err := state.Load(b.Context(), &reader, &restored); err != nil {
			b.Fatal(err)
		}
	}
	b.ReportMetric(float64(len(encoded)), "wire-B/op")
}
