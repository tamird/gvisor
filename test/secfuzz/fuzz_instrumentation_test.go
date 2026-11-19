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
	"slices"
	"testing"
	_ "unsafe"

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
	clear(counters)
	secfuzz.InstrumentationProbe(false)
	baseline := slices.Clone(counters)
	clear(counters)
	secfuzz.InstrumentationProbe(false)
	if !slices.Equal(counters, baseline) {
		t.Fatal("the repeated false control changed fuzz coverage")
	}
	clear(counters)
	secfuzz.InstrumentationProbe(true)
	newEdges := 0
	for i, count := range counters {
		if count != 0 && baseline[i] == 0 {
			newEdges++
		}
	}
	if newEdges == 0 {
		t.Fatal("the true branch added no fuzz coverage")
	}
	t.Logf("fuzz counters: %d bytes, %d new true-branch edges", len(counters), newEdges)
}
