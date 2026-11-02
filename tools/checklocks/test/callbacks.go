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

package test

import (
	"slices"
	"sync"
)

// callbackEntries exercises lock checking through synchronous slice callbacks.
type callbackEntries struct {
	mu sync.Mutex
	// +checklocks:mu
	entries map[string]struct{}
}

// +checklocks:e.mu
func (e *callbackEntries) hasKeyLocked(key string) bool {
	_, found := e.entries[key]
	return found
}

// +checklocks:e.mu
func (e *callbackEntries) containsAnyLocked(keys []string) bool {
	return slices.ContainsFunc(keys, e.hasKeyLocked)
}

// +checklocks:e.mu
func (e *callbackEntries) indexAnyLocked(keys []string) int {
	return slices.IndexFunc(keys, e.hasKeyLocked)
}

func (e *callbackEntries) temporarilyReleaseInPredicate(keys []string) {
	e.mu.Lock()
	slices.ContainsFunc(keys, func(key string) bool {
		e.mu.Unlock()
		e.mu.Lock()
		return e.hasKeyLocked(key)
	})
	delete(e.entries, "")
	e.mu.Unlock()
}

// +checklocksrelease:globalMu
func unlockBeforeYield(yield func(int) bool) {
	globalMu.Unlock()
	if yield(2) {
		yield(1)
	}
}

// +checklocksrelease:globalMu
func testSortedFunc(order int) []int {
	// SortedFunc collects the entire sequence before it calls the comparator.
	// Checklocks cannot infer the iterator's unlock through SortedFunc.
	return slices.SortedFunc(unlockBeforeYield, func(a, b int) int { // +checklocksfail=return with unexpected locks held
		if otherStruct.guardedField1 == order { // +checklocksfail=invalid field access
			return b - a
		}
		return a - b
	})
}
