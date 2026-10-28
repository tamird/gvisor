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
	"context"

	"gvisor.dev/gvisor/pkg/state"
)

// +stateify savable
type genericContainer struct {
	v any
}

// +stateify savable
type afterLoadStruct struct {
	v int `state:"nosave"`
}

func (a *afterLoadStruct) afterLoad(context.Context) {
	a.v++
}

// +stateify savable
type convertedContainers struct {
	fromMap       int `state:".(map[int]afterLoadStruct)"`
	fromInterface int `state:".(genericContainer)"`
}

func (c *convertedContainers) saveFromMap() map[int]afterLoadStruct {
	return map[int]afterLoadStruct{7: {v: c.fromMap}}
}

func (c *convertedContainers) loadFromMap(_ context.Context, m map[int]afterLoadStruct) {
	c.fromMap = m[7].v
}

func (c *convertedContainers) saveFromInterface() genericContainer {
	return genericContainer{v: afterLoadStruct{v: c.fromInterface}}
}

func (c *convertedContainers) loadFromInterface(_ context.Context, value genericContainer) {
	c.fromInterface = value.v.(afterLoadStruct).v
}

// +stateify type
type earlyAfterLoad struct {
	value              int
	observed           int `state:"nosave"`
	seenFromConversion int `state:"nosave"`
}

func (a *earlyAfterLoad) StateSave(s state.Sink) {
	v := a.value
	s.SaveValue(0, &v)
}

func (a *earlyAfterLoad) StateLoad(_ context.Context, s state.Source) {
	s.AfterLoad(func() { a.observed = a.value })
	s.LoadValue(0, new(*int), func(v any) {
		a.value = *v.(*int)
		s.AfterLoad(func() { a.seenFromConversion = a.value })
	})
}

// +stateify savable
type sharedSliceLoad struct {
	elements []afterLoadStruct
	observed int  `state:".([]afterLoadStruct)"`
	shared   bool `state:"nosave"`
}

func (s *sharedSliceLoad) saveObserved() []afterLoadStruct {
	return s.elements
}

func (s *sharedSliceLoad) loadObserved(_ context.Context, elements []afterLoadStruct) {
	s.observed = elements[0].v
	s.shared = &s.elements[0] == &elements[0]
}

// +stateify savable
type valueLoadStruct struct {
	v int `state:".(int64)"`
}

func (v *valueLoadStruct) saveV() int64 {
	return int64(v.v) // Save as int64.
}

func (v *valueLoadStruct) loadV(_ context.Context, value int64) {
	v.v = int(value) // Load as int.
}

// +stateify savable
type cycleStruct struct {
	c *cycleStruct
}

// +stateify savable
type badCycleStruct struct {
	b *badCycleStruct `state:"wait"`
}

func (b *badCycleStruct) afterLoad(context.Context) {
	if b.b != b {
		// This is not executable, since AfterLoad requires that the
		// object and all dependencies are complete. This should cause
		// a deadlock error during load.
		panic("badCycleStruct.afterLoad called")
	}
}

// +stateify savable
type branchingCycle struct {
	reachable   *branchingCycle
	waitFor     *branchingCycle `state:"wait"`
	alsoWaitFor *branchingCycle `state:"wait"`
}
