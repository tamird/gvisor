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

package backtrace

import (
	"regexp"
	"testing"
)

func TestDumpStack(t *testing.T) {
	output, err := dumpStack()
	if err != nil {
		t.Fatalf("dumpStack(): %v", err)
	}
	// Symbol names may be absent from a static executable. The addresses must
	// still distinguish the DPDK frame from its caller across the cgo boundary.
	pcs := make(map[string]bool)
	for _, pc := range regexp.MustCompile(`\[0x0*[1-9a-f][0-9a-f]*\b`).FindAllString(output, -1) {
		pcs[pc] = true
	}
	if len(pcs) < 2 {
		t.Fatalf("stack trace contains %d distinct frame addresses, want at least two:\n%s", len(pcs), output)
	}
}
