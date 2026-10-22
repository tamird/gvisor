// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package state

import (
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"io"
	"testing"
)

// Both benchmark arms use these exact types, methods and graph. Only the
// common-engine arm supplies internal typed storage operations for them.
type commonEngineBenchmarkInt int64
type commonEngineBenchmarkMap map[commonEngineBenchmarkInt]commonEngineBenchmarkSlice
type commonEngineBenchmarkSlice []commonEngineBenchmarkMap

func (*commonEngineBenchmarkMap) StateTypeName() string { return "state.commonEngineBenchmarkMap" }
func (*commonEngineBenchmarkMap) StateFields() []string { return nil }
func (*commonEngineBenchmarkSlice) StateTypeName() string {
	return "state.commonEngineBenchmarkSlice"
}
func (*commonEngineBenchmarkSlice) StateFields() []string { return nil }

type commonEngineBenchmarkRoot struct {
	graph commonEngineBenchmarkMap
	value commonEngineBenchmarkInt
}

func (*commonEngineBenchmarkRoot) StateTypeName() string { return "state.commonEngineBenchmarkRoot" }
func (*commonEngineBenchmarkRoot) StateFields() []string { return []string{"graph", "value"} }
func (v *commonEngineBenchmarkRoot) StateSave(s Sink) {
	s.Save(0, &v.graph)
	s.Save(1, &v.value)
}
func (v *commonEngineBenchmarkRoot) StateLoad(_ context.Context, s Source) {
	s.LoadWait(0, &v.graph)
	s.Load(1, &v.value)
}

func init() {
	Register((*commonEngineBenchmarkMap)(nil))
	Register((*commonEngineBenchmarkSlice)(nil))
	Register((*commonEngineBenchmarkRoot)(nil))
}

func commonEngineBenchmarkGraph() commonEngineBenchmarkRoot {
	leaf := commonEngineBenchmarkMap{101: nil}
	shared := commonEngineBenchmarkSlice{leaf, leaf}
	graph := make(commonEngineBenchmarkMap, 1024)
	for i := range 1024 {
		graph[commonEngineBenchmarkInt(i)] = shared
	}
	return commonEngineBenchmarkRoot{graph: graph, value: 17}
}

func checkCommonEngineBenchmark(b *testing.B, restored commonEngineBenchmarkRoot, encoded []byte) {
	b.Helper()
	if restored.value != 17 || len(restored.graph) != 1024 {
		b.Fatalf("restored value/map size = %d/%d, want 17/1024", restored.value, len(restored.graph))
	}
	shared := restored.graph[0]
	if len(shared) != 2 || cap(shared) != 2 {
		b.Fatalf("restored slice len/cap = %d/%d, want 2/2", len(shared), cap(shared))
	}
	for i := range 1024 {
		slice, ok := restored.graph[commonEngineBenchmarkInt(i)]
		if !ok || len(slice) != 2 || cap(slice) != 2 {
			b.Fatalf("restored map[%d] slice len/cap = %d/%d, want 2/2", i, len(slice), cap(slice))
		}
		if &slice[0] != &shared[0] {
			b.Fatalf("restored map[%d] backing array = %p, want %p", i, &slice[0], &shared[0])
		}
		for j, leaf := range slice {
			value, ok := leaf[101]
			if len(leaf) != 1 || !ok || value != nil {
				b.Fatalf("restored map[%d][%d] = %v, want {101: nil}", i, j, leaf)
			}
		}
	}
	delete(shared[0], 101)
	if len(shared[1]) != 0 {
		b.Fatal("restored leaf maps do not alias")
	}
	// Map iteration can change the digest; retain it without requiring equality.
	fmt.Printf("state-wire-sha256[manual]: %x\n", sha256.Sum256(encoded))
	b.ReportMetric(float64(len(encoded)), "wire-B/op")
}

func BenchmarkCommonEngineSave(b *testing.B) {
	input := commonEngineBenchmarkGraph()
	b.ReportAllocs()
	for b.Loop() {
		if _, err := Save(b.Context(), io.Discard, &input); err != nil {
			b.Fatal(err)
		}
	}
	var encoded bytes.Buffer
	if _, err := Save(b.Context(), &encoded, &input); err != nil {
		b.Fatal(err)
	}
	data := encoded.Bytes()
	var restored commonEngineBenchmarkRoot
	if _, err := Load(b.Context(), &encoded, &restored); err != nil {
		b.Fatal(err)
	}
	checkCommonEngineBenchmark(b, restored, data)
}

func BenchmarkCommonEngineLoad(b *testing.B) {
	input := commonEngineBenchmarkGraph()
	var encoded bytes.Buffer
	if _, err := Save(b.Context(), &encoded, &input); err != nil {
		b.Fatal(err)
	}
	var restored commonEngineBenchmarkRoot
	var reader bytes.Reader
	b.ReportAllocs()
	for b.Loop() {
		reader.Reset(encoded.Bytes())
		if _, err := Load(b.Context(), &reader, &restored); err != nil {
			b.Fatal(err)
		}
	}
	checkCommonEngineBenchmark(b, restored, encoded.Bytes())
}
