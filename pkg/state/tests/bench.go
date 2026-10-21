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
type benchStruct struct {
	B *benchStruct // Must be exported for gob.
}

func (b *benchStruct) afterLoad(context.Context) {
	// Do nothing, just force scheduling.
}

// benchAdopted is first discovered through a pointer, then as an array element.
//
// These diagnostic manual methods preserve the existing adoption workload
// while exercising direct operations before changing the whole generator.
// +stateify type
type benchAdopted struct {
	Value int64
	saves *int `state:"nosave"`
}

func (b *benchAdopted) StateSave(s state.Sink) {
	b.beforeSave()
	s.Save(0, &b.Value)
}

func (b *benchAdopted) StateLoad(_ context.Context, s state.Source) {
	state.LoadField(s, 0, &b.Value, state.IntOps(&b.Value))
}

func (b *benchAdopted) beforeSave() {
	if b.saves != nil {
		*b.saves++
	}
}

// +stateify type
type benchAdoptionParent struct {
	Values []benchAdopted
	Next   *benchAdoptionParent
}

func (b *benchAdoptionParent) StateSave(s state.Sink) {
	s.Save(0, &b.Values)
	s.Save(1, &b.Next)
}

func (b *benchAdoptionParent) StateLoad(_ context.Context, s state.Source) {
	value := state.SliceTypes(b.Values)
	state.LoadField(s, 0, &b.Values, state.SliceOps(&b.Values, state.ValueOps(&value)))
	state.LoadField(s, 1, &b.Next, state.PointerOps(&b.Next, state.ValueOps(b)))
}

// +stateify type
type benchAdoption struct {
	Pointers []*benchAdopted
	Parent   *benchAdoptionParent
}

func (b *benchAdoption) StateSave(s state.Sink) {
	s.Save(0, &b.Pointers)
	s.Save(1, &b.Parent)
}

func (b *benchAdoption) StateLoad(_ context.Context, s state.Source) {
	pointer := state.SliceTypes(b.Pointers)
	var value benchAdopted
	state.LoadField(s, 0, &b.Pointers, state.SliceOps(&b.Pointers, state.PointerOps(&pointer, state.ValueOps(&value))))
	var parent benchAdoptionParent
	state.LoadField(s, 1, &b.Parent, state.PointerOps(&b.Parent, state.ValueOps(&parent)))
}
