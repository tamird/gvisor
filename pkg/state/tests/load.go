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

// These ordinary fields exercise legacy wire operations through generated
// StateLoad, rather than only through the manual Source adapter.
// +stateify savable
type restoreLegacyFields struct {
	integer       int64
	text          string
	slice         []int64
	pointer       *int64
	array         [2]int64
	mapping       map[int]int64
	signed        int8
	unsigned      uint8
	narrowFloat   float32
	narrowComplex complex64
}

// +stateify savable
type afterLoadStruct struct {
	v int `state:"nosave"`
}

func (a *afterLoadStruct) afterLoad(context.Context) {
	a.v++
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

// replayLoadStruct exercises manual non-stream-order access and a Source that
// survives its callback and the enclosing Load. The omitted field still has to
// be fully parsed before StateLoad runs.
// +stateify type
type replayLoadStruct struct {
	// Manual capture and replay must not traverse this nil intermediate
	// pointer to invoke inherited hooks.
	*replayLoadMiddle `state:"nosave"`
	first             int64
	second            string
	omitted           int64
	source            state.Source `state:"nosave"`
	repeated          int64        `state:"nosave"`
	after             string       `state:"nosave"`
	calls             int          `state:"nosave"`
}

type replayLoadMiddle struct{ afterLoadStruct }

func (r *replayLoadStruct) StateSave(s state.Sink) {
	s.Save(0, &r.first)
	s.Save(1, &r.second)
	s.Save(2, &r.omitted)
}

func (r *replayLoadStruct) StateLoad(_ context.Context, s state.Source) {
	r.calls++
	s.Load(1, &r.second)
	s.Load(0, &r.first)
	s.Load(0, &r.repeated)
	r.source = s
	s.AfterLoad(func() { s.Load(1, &r.after) })
}

// sharedLayoutFields is first loaded concurrently with different wire field
// orders. Each Load reconciles its own schema for the same recursive Go type.
// +stateify savable
type sharedLayoutFields struct {
	_      uint64
	first  int64
	second int64
	text   string
	next   *sharedLayoutFields
	loads  int `state:"nosave"`
}

func (s *sharedLayoutFields) afterLoad(context.Context) { s.loads++ }
