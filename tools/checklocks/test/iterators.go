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
	"strings"
	"sync"

	"gvisor.dev/gvisor/tools/checklocks/test/crosspkg"
)

type iteratorEntries struct {
	mu sync.Mutex
	// +checklocks:mu
	entries []string
}

// This is the original reproducer from https://github.com/google/gvisor/issues/12176.
func testRangeFunctionSplitSeq(e *iteratorEntries, s string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	for item := range strings.SplitSeq(s, " ") {
		e.entries = append(e.entries, item)
	}
}

func testRangeFunctionImportedFactory(tc *oneGuardStruct) {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	total := 0
	for value := range crosspkg.Integers() {
		if value == 0 {
			continue
		}
		total += value
		tc.guardedField = total
		if total > 10 {
			break
		}
	}
	tc.guardedField = total
}

func testRangeFunctionMissingLock(tc *oneGuardStruct) {
	for value := range crosspkg.Integers() {
		tc.guardedField = value // +checklocksfail=invalid field access
	}
}

func testRangeFunctionImportedTransientUnlock() {
	crosspkg.FooMu.Lock()
	defer crosspkg.FooMu.Unlock()
	for value := range crosspkg.TransientUnlockSeq() {
		crosspkg.Foo = value // +checklocksfail=invalid field access
	}
}

// +checklocks:globalStruct.mu
func transientUnlockSeq(yield func(int) bool) {
	globalStruct.mu.Unlock()
	yield(1)
	globalStruct.mu.Lock()
}

func testRangeFunctionTransientUnlock() {
	globalStruct.mu.Lock()
	defer globalStruct.mu.Unlock()
	for value := range transientUnlockSeq {
		globalStruct.guardedField = value // +checklocksfail=invalid field access
	}
}

func testRangeFunctionUnknown(tc *oneGuardStruct, seq func(func(int) bool)) {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	for value := range seq {
		tc.guardedField = value // +checklocksfail=invalid field access
	}
}

func testRangeFunctionPointerMutation(tc, other *oneGuardStruct) {
	p := tc
	tc.mu.Lock()
	defer tc.mu.Unlock()
	for value := range crosspkg.Integers() {
		p.guardedField = value
		p = other // +checklocksfail=range body changes a lock identity
	}
	p.guardedField = 1 // +checklocksfail=invalid field access|invalid field access
}

func testRangeFunctionIndexMutation(values []*oneGuardStruct, index int) {
	i := index
	values[i].mu.Lock()
	defer values[i].mu.Unlock()
	for value := range crosspkg.Integers() {
		values[i].guardedField = value
		i++ // +checklocksfail=range body changes a lock identity
	}
}

func testRangeFunctionDefer(tc *oneGuardStruct) {
	for range crosspkg.Integers() {
		tc.mu.Lock()
		defer tc.mu.Unlock() // +checklocksfail=defer in a range-over-function body is not supported
	}
}

func swapIteratorPointer(p **oneGuardStruct, other *oneGuardStruct) {
	*p = other
}

func testRangeFunctionHelperMutation(tc, other *oneGuardStruct) {
	p := tc
	tc.mu.Lock()
	defer tc.mu.Unlock()
	for value := range crosspkg.Integers() {
		p.guardedField = value
		swapIteratorPointer(&p, other) // +checklocksfail=range body changes a lock identity
	}
}

func testRangeFunctionNestedClosureMutation(tc, other *oneGuardStruct) {
	p := tc
	tc.mu.Lock()
	defer tc.mu.Unlock()
	for value := range crosspkg.Integers() {
		p.guardedField = value
		func() {
			p = other // +checklocksfail=range body changes a lock identity
		}()
	}
}

func testRangeFunctionConditionalLeak(tc *oneGuardStruct, acquire bool) {
	for range crosspkg.Integers() { // +checklocksfail=range body changes lock state|incompatible return states
		if acquire {
			tc.mu.Lock()
		}
	}
}

var iteratorIndex int

type iteratorSelector struct {
	index int
}

func testRangeFunctionGlobalSelector(values []*oneGuardStruct) {
	iteratorIndex = 0
	values[iteratorIndex].mu.Lock()
	defer values[iteratorIndex].mu.Unlock()
	for range crosspkg.Integers() {
		iteratorIndex++ // +checklocksfail=range body changes a lock identity
	}
}

func testRangeFunctionFieldSelector(values []*oneGuardStruct, selector *iteratorSelector) {
	selector.index = 0
	values[selector.index].mu.Lock()
	defer values[selector.index].mu.Unlock()
	for range crosspkg.Integers() {
		selector.index++ // +checklocksfail=range body changes a lock identity
	}
}

func bumpIteratorSelector(index *int) {
	(*index)++
}

func testRangeFunctionScalarHelper(values []*oneGuardStruct) {
	index := 0
	values[index].mu.Lock()
	defer values[index].mu.Unlock()
	for range crosspkg.Integers() {
		bumpIteratorSelector(&index) // +checklocksfail=range body changes a lock identity
	}
}

func testRangeFunctionCapturedSelector(values []*oneGuardStruct) {
	index := 0
	seq := func(yield func(int) bool) {
		index++
		yield(index)
	}
	values[index].mu.Lock()
	defer values[index].mu.Unlock()
	for value := range seq {
		values[index].guardedField = value // +checklocksfail=invalid field access
	}
}

func mutatingScalarSeq(index *int) func(func(int) bool) {
	return func(yield func(int) bool) {
		bumpIteratorSelector(index)
		yield(*index)
	}
}

func testRangeFunctionExternalSelector(values []*oneGuardStruct) {
	index := 0
	values[index].mu.Lock()
	defer values[index].mu.Unlock()
	for value := range mutatingScalarSeq(&index) {
		values[index].guardedField = value // +checklocksfail=invalid field access
	}
}

// The synthetic yield uses this named callback parameter. It must not alias
// an equally named parameter in the enclosing function.
func pointerSeq(value *oneGuardStruct) func(func(v *oneGuardStruct) bool) {
	return func(yield func(*oneGuardStruct) bool) {
		yield(value)
	}
}

func testRangeFunctionShadowedParameter(v, other *oneGuardStruct) {
	v.mu.Lock()
	defer v.mu.Unlock()
	for v := range pointerSeq(other) {
		v.guardedField = 1 // +checklocksfail=invalid field access
	}
}
