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

package atomicptr

import "testing"

func TestAtomicPtrInlineProbes(t *testing.T) {
	var before AtomicPtrBeforeInt
	var after AtomicPtrInt
	first, second := 42, 100
	storeBefore(&before, &first)
	storeAfter(&after, &first)
	if got := loadBefore(&before); got != &first {
		t.Fatalf("before Load = %p, want %p", got, &first)
	}
	if got := loadAfter(&after); got != &first {
		t.Fatalf("after Load = %p, want %p", got, &first)
	}
	if got := swapBefore(&before, &second); got != &first {
		t.Fatalf("before Swap = %p, want %p", got, &first)
	}
	if got := swapAfter(&after, &second); got != &first {
		t.Fatalf("after Swap = %p, want %p", got, &first)
	}
	if got := loadBefore(&before); got != &second {
		t.Fatalf("before final Load = %p, want %p", got, &second)
	}
	if got := loadAfter(&after); got != &second {
		t.Fatalf("after final Load = %p, want %p", got, &second)
	}
	if got := loadControl(&after); got != &second {
		t.Fatalf("control Load = %p, want %p", got, &second)
	}
}
