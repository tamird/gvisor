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

package secfuzz_test

import (
	"encoding/json"
	"runtime"
	"slices"
	"testing"
	_ "unsafe"

	"gvisor.dev/gvisor/pkg/bpf"
	"gvisor.dev/gvisor/test/secfuzz"
)

// fuzzCounters reads the counter range owned by the pinned Go fuzzing engine.
//
//go:linkname fuzzCounters internal/fuzz.coverage
func fuzzCounters() []byte

func TestFuzzInstrumentation(t *testing.T) {
	counters := fuzzCounters()
	if len(counters) == 0 {
		t.Fatal("the compiler emitted no fuzz coverage counters")
	}
	programs := [...]struct {
		name   string
		record func([]bool)
	}{
		{"first", secfuzz.RecordFirstCoverage},
		{"second", secfuzz.RecordSecondCoverage},
	}
	const limit = bpf.MaxInstructions
	candidates := make([][][]int, len(programs))
	owners := make([]int, len(counters))
	for p, program := range programs {
		covered := make([]bool, limit)
		clear(counters)
		program.record(covered)
		baseline := slices.Clone(counters)
		clear(counters)
		program.record(covered)
		if !slices.Equal(counters, baseline) {
			t.Fatalf("%s: repeated false control changed fuzz coverage", program.name)
		}
		candidates[p] = make([][]int, limit)
		// A fixed length prevents dispatcher coverage from identifying the PC.
		for pc := range covered {
			covered[pc] = true
			clear(counters)
			program.record(covered)
			for offset, count := range counters {
				if count == 0 {
					continue
				}
				// Count every positive hit, including hits also in the control.
				owners[offset]++
				if baseline[offset] == 0 {
					candidates[p][pc] = append(candidates[p][pc], offset)
				}
			}
			covered[pc] = false
		}
	}
	witnesses := make(map[string][]int, len(programs))
	for p, program := range programs {
		witnesses[program.name] = make([]int, limit)
		for pc := range limit {
			witness := -1
			for _, offset := range candidates[p][pc] {
				if owners[offset] == 1 {
					witness = offset
					break
				}
			}
			if witness < 0 {
				t.Fatalf("%s instruction %d has no globally exclusive counter (%d true-only candidates)", program.name, pc, len(candidates[p][pc]))
			}
			witnesses[program.name][pc] = witness
		}
	}
	proof, err := json.Marshal(struct {
		Runtime      string           `json:"runtime"`
		CounterBytes int              `json:"counter_bytes"`
		Witnesses    map[string][]int `json:"witnesses"`
	}{
		Runtime:      runtime.Version(),
		CounterBytes: len(counters),
		Witnesses:    witnesses,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Log(string(proof))
}
