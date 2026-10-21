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

package benchmark

import (
	"bytes"
	"context"
	"crypto/sha256"
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

// buildMapFieldObject uses an existing generated owner while keeping its
// interface-valued entries genuinely dynamic.
func buildMapFieldObject(n int) any {
	m := &mapContainer{v: make(map[int]any)}
	b := new(benchStruct)
	for i := 0; i < n; i++ {
		m.v[i] = b
	}
	return m
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

// buildAdoptionObject controls whether the enclosing array is discovered before
// or after its children's save hooks. Pointer-array capture queues the children;
// one additional intermediary delays array discovery until those children save.
func buildAdoptionObject(n int, captured bool) *benchAdoption {
	values := make([]benchAdopted, n)
	pointers := make([]*benchAdopted, n)
	for i := range values {
		values[i].Value = int64(i + 1)
		pointers[i] = &values[i]
	}
	parent := &benchAdoptionParent{Values: values}
	if captured {
		parent = &benchAdoptionParent{Next: parent}
	}
	return &benchAdoption{Pointers: pointers, Parent: parent}
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
	"map_field": {
		New: buildMapFieldObject,
	},
	"slice": {
		New: buildSliceObject,
	},
	"adopt_uncaptured": {
		New: func(n int) any { return buildAdoptionObject(n, false) },
	},
	"adopt_captured": {
		New: func(n int) any { return buildAdoptionObject(n, true) },
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
					if _, err := algoInfo.Save(b.Context(), io.Discard, v); err != nil {
						b.Errorf("save failed: %v", err)
					}
				}
				if algoName == "state" {
					var encoded bytes.Buffer
					if _, err := state.Save(b.Context(), &encoded, v); err != nil {
						b.Fatal(err)
					}
					data := encoded.Bytes()
					zeroBenchmarkObject(b, v)
					if _, err := state.Load(b.Context(), &encoded, v); err != nil {
						b.Fatal(err)
					}
					validateBenchmarkObject(b, objName, v)
					recordBenchmarkWire(b, objName, data)
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
				if _, err := algoInfo.Save(b.Context(), buf, v); err != nil {
					b.Errorf("save failed: %v", err)
				}
				zeroBenchmarkObject(b, v)
				b.ReportAllocs()
				var r bytes.Reader
				for b.Loop() {
					r.Reset(buf.Bytes())
					if _, err := algoInfo.Load(b.Context(), &r, v); err != nil {
						b.Errorf("load failed: %v", err)
					}
				}
				if algoName == "state" {
					validateBenchmarkObject(b, objName, v)
					recordBenchmarkWire(b, objName, buf.Bytes())
				}
			})
		}
	}
}

// Clear once outside measurement so a no-op Load cannot pass final validation.
// Repeated iterations retain the existing reused-destination timing boundary.
func zeroBenchmarkObject(b *testing.B, object any) {
	b.Helper()
	switch value := object.(type) {
	case *benchStruct:
		*value = benchStruct{}
	case *map[int]*benchStruct:
		*value = nil
	case *[]*benchStruct:
		*value = nil
	case *mapContainer:
		*value = mapContainer{}
	case *benchAdoption:
		*value = benchAdoption{}
	default:
		b.Fatalf("no benchmark reset for %T", object)
	}
}

// Validation and wire identity collection run after B.Loop stops the timer.
func recordBenchmarkWire(b *testing.B, name string, encoded []byte) {
	b.Helper()
	fmt.Printf("state-wire-sha256[%s]: %x\n", name, sha256.Sum256(encoded))
	b.ReportMetric(float64(len(encoded)), "wire-B/op")
}

func validateBenchmarkObject(b *testing.B, name string, object any) {
	b.Helper()
	switch name {
	case "ptr":
		p := object.(*benchStruct)
		for range 1025 {
			if p == nil {
				b.Fatal("restored pointer chain is short")
			}
			p = p.B
		}
		if p != nil {
			b.Fatal("restored pointer chain is long or cyclic")
		}
	case "map":
		m := *object.(*map[int]*benchStruct)
		if got, want := len(m), 1024; got != want {
			b.Fatalf("restored map length = %d, want %d", got, want)
		}
		shared := m[0]
		if shared == nil || shared.B != nil {
			b.Fatal("restored map value changed")
		}
		for i := range 1024 {
			if got, want := m[i], shared; got != want {
				b.Fatalf("restored map value %d = %p, want shared %p", i, got, want)
			}
		}
	case "map_field":
		m := object.(*mapContainer)
		if got, want := m.prefix, 0; got != want {
			b.Fatalf("restored map prefix = %d, want %d", got, want)
		}
		if got, want := len(m.v), 1024; got != want {
			b.Fatalf("restored map length = %d, want %d", got, want)
		}
		shared, ok := m.v[0].(*benchStruct)
		if !ok || shared == nil || shared.B != nil {
			b.Fatal("restored dynamic map value changed")
		}
		for i := range 1024 {
			if got, want := m.v[i], any(shared); got != want {
				b.Fatalf("restored map value %d = %v, want shared %p", i, got, shared)
			}
		}
	case "adopt_captured", "adopt_uncaptured":
		graph := object.(*benchAdoption)
		parent := graph.Parent
		if parent == nil {
			b.Fatal("missing restored adoption parent")
		}
		if name == "adopt_captured" {
			parent = parent.Next
		}
		if parent == nil || len(parent.Values) != 1024 || len(graph.Pointers) != 1024 {
			b.Fatal("restored adoption graph has wrong dimensions")
		}
		for i, child := range graph.Pointers {
			if got, want := child, &parent.Values[i]; got != want {
				b.Fatalf("restored child %d = %p, want %p", i, got, want)
			}
			if got, want := child.Value, int64(i+1); got != want {
				b.Fatalf("restored child %d value = %d, want %d", i, got, want)
			}
		}
	case "slice":
		s := *object.(*[]*benchStruct)
		if got, want := len(s), 1024; got != want {
			b.Fatalf("restored slice length = %d, want %d", got, want)
		}
		if s[0] == nil || s[0].B != nil {
			b.Fatal("restored slice value changed")
		}
		for i, v := range s {
			if got, want := v, s[0]; got != want {
				b.Fatalf("restored slice value %d = %p, want shared %p", i, got, want)
			}
		}
	default:
		b.Fatalf("no benchmark validation for %q", name)
	}
}

// controlMessageBenchmark uses a real timestamp-bearing state type.
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
	original, encoded := controlMessageBenchmark(b)
	var restored tcpip.ReceivableControlMessages
	var reader bytes.Reader
	b.ReportAllocs()
	for b.Loop() {
		reader.Reset(encoded)
		if _, err := state.Load(b.Context(), &reader, &restored); err != nil {
			b.Fatal(err)
		}
	}
	if !restored.HasTimestamp || !restored.Timestamp.Equal(original.Timestamp) {
		b.Fatalf("restored control message = %+v, want timestamp %v", restored, original.Timestamp)
	}
	b.ReportMetric(float64(len(encoded)), "wire-B/op")
}

// BenchmarkFirstRestore is selected one leaf at a time with benchtime=1x and
// count=1 in a fresh process. Setup saves the graph but never loads it, so the
// timed operation includes first-use restoration metadata. Repeated iterations
// are valid operations but do not have that first-use interpretation.
func BenchmarkFirstRestore(b *testing.B) {
	b.Run("empty", func(b *testing.B) {
		firstRestore(b, struct{}{}, func(*struct{}) {})
	})
	b.Run("ordinary", func(b *testing.B) {
		firstRestore(b, benchAdopted{Value: 17}, func(restored *benchAdopted) {
			if got, want := restored.Value, int64(17); got != want {
				b.Fatalf("restored value = %d, want %d", got, want)
			}
		})
	})
	b.Run("retained_source", func(b *testing.B) {
		firstRestore(b, benchmarkRetainedSource{first: 17, second: "retained field", omitted: 99}, func(restored *benchmarkRetainedSource) {
			if got, want := restored.calls, 1; got != want {
				b.Fatalf("StateLoad calls = %d, want %d", got, want)
			}
			if got, want := restored.first, int64(17); got != want {
				b.Fatalf("restored first = %d, want %d", got, want)
			}
			if got, want := restored.after, "retained field"; got != want {
				b.Fatalf("AfterLoad replay = %q, want %q", got, want)
			}
			var replay int64
			restored.source.Load(0, &replay)
			if got, want := replay, int64(17); got != want {
				b.Fatalf("retained Source replay = %d, want %d", got, want)
			}
		})
	})
	b.Run("control_message", func(b *testing.B) {
		original := tcpip.ReceivableControlMessages{
			HasTimestamp: true,
			Timestamp:    time.Date(2026, time.October, 7, 12, 0, 0, 123456789, time.UTC),
		}
		firstRestore(b, original, func(restored *tcpip.ReceivableControlMessages) {
			if got, want := restored.HasTimestamp, true; got != want {
				b.Fatalf("HasTimestamp = %t, want %t", got, want)
			}
			if got, want := restored.Timestamp, original.Timestamp; !got.Equal(want) {
				b.Fatalf("restored timestamp = %v, want %v", got, want)
			}
		})
	})
}

func firstRestore[T any](b *testing.B, original T, check func(*T)) {
	b.Helper()
	var encoded bytes.Buffer
	if _, err := state.Save(b.Context(), &encoded, &original); err != nil {
		b.Fatal(err)
	}
	var restored, zero T
	var reader bytes.Reader
	b.ReportAllocs()
	for b.Loop() {
		restored = zero
		reader.Reset(encoded.Bytes())
		if _, err := state.Load(b.Context(), &reader, &restored); err != nil {
			b.Fatal(err)
		}
	}
	check(&restored)
	b.ReportMetric(float64(encoded.Len()), "wire-B/op")
}
