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
	"context"
	"gvisor.dev/gvisor/pkg/state"
)

// +stateify savable
type benchStruct struct {
	B *benchStruct // Must be exported for gob.
}

func (b *benchStruct) afterLoad(context.Context) {
	// Do nothing, just force scheduling.
}

// benchAdopted is first discovered through a pointer, then as an array element.
//
// +stateify savable
type benchAdopted struct {
	Value int64
	saves *int `state:"nosave"`
}

func (b *benchAdopted) beforeSave() {
	if b.saves != nil {
		*b.saves++
	}
}

// +stateify savable
type benchAdoptionParent struct {
	Values []benchAdopted
	Next   *benchAdoptionParent
}

// +stateify savable
type benchAdoption struct {
	Pointers []*benchAdopted
	Parent   *benchAdoptionParent
}

// benchmarkRetainedSource exercises manual non-stream-order access and a Source that
// survives its callback and the enclosing Load. The omitted field still has to
// be fully parsed before StateLoad runs.
// +stateify type
type benchmarkRetainedSource struct {
	first    int64
	second   string
	omitted  int64
	source   state.Source `state:"nosave"`
	repeated int64        `state:"nosave"`
	after    string       `state:"nosave"`
	calls    int          `state:"nosave"`
}

func (r *benchmarkRetainedSource) StateSave(s state.Sink) {
	s.Save(0, &r.first)
	s.Save(1, &r.second)
	s.Save(2, &r.omitted)
}

func (r *benchmarkRetainedSource) StateLoad(_ context.Context, s state.Source) {
	r.calls++
	s.Load(1, &r.second)
	s.Load(0, &r.first)
	s.Load(0, &r.repeated)
	r.source = s
	s.AfterLoad(func() { s.Load(1, &r.after) })
}
