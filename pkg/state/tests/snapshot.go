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

package tests

import (
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/wire"
)

// These explicit records model generated output before expanding stateify's
// type metadata inputs. They copy saved fields only, never entire source types.
type integerSnapshot struct{ value int64 }

func emitInteger(w *wire.Writer, s *integerSnapshot) {
	wire.SaveIntField(w, s.value)
}

func (v *inner) StateSave(s state.Sink) {
	v.beforeSave()
	snapshot := state.BeginSnapshot(s, emitInteger)
	snapshot.value = v.v
}

func (v *savedFieldValue) StateSave(s state.Sink) {
	v.beforeSave()
	snapshot := state.BeginSnapshot(s, emitInteger)
	snapshot.value = v.v
}

type childSnapshot struct{ child wire.Object }

func emitChild(w *wire.Writer, s *childSnapshot) { wire.Save(w, s.child) }

func (v *innerFieldValue) StateSave(s state.Sink) {
	v.beforeSave()
	snapshot := state.BeginSnapshot(s, emitChild)
	// Preserve the existing custom SaveValue call, including its once-only
	// allocation and the graph identity of the returned pointer.
	value := v.saveV()
	s.CaptureValue(value, &snapshot.child)
}

func (v *outerSame) StateSave(s state.Sink) {
	v.beforeSave()
	snapshot := state.BeginSnapshot(s, emitChild)
	s.Capture(&v.inner, &snapshot.child)
}

type innerIntegerSnapshot struct {
	child wire.Object
	value int64
}

func emitInnerInteger(w *wire.Writer, s *innerIntegerSnapshot) {
	wire.Save(w, s.child)
	wire.SaveIntField(w, s.value)
}

func emitIntegerInner(w *wire.Writer, s *innerIntegerSnapshot) {
	wire.SaveIntField(w, s.value)
	wire.Save(w, s.child)
}

func (v *outerFieldFirst) StateSave(s state.Sink) {
	v.beforeSave()
	snapshot := state.BeginSnapshot(s, emitInnerInteger)
	s.Capture(&v.inner, &snapshot.child)
	snapshot.value = v.v
}

func (v *outerFieldSecond) StateSave(s state.Sink) {
	v.beforeSave()
	snapshot := state.BeginSnapshot(s, emitIntegerInner)
	snapshot.value = v.v
	s.Capture(&v.inner, &snapshot.child)
}

type twoChildrenSnapshot struct{ first, second wire.Object }

func emitTwoChildren(w *wire.Writer, s *twoChildrenSnapshot) {
	wire.Save(w, s.first)
	wire.Save(w, s.second)
}

func (v *system) StateSave(s state.Sink) {
	v.beforeSave()
	snapshot := state.BeginSnapshot(s, emitTwoChildren)
	s.Capture(&v.v1, &snapshot.first)
	s.Capture(&v.v2, &snapshot.second)
}

type threeChildrenSnapshot struct{ first, second, third wire.Object }

func emitThreeChildren(w *wire.Writer, s *threeChildrenSnapshot) {
	wire.Save(w, s.first)
	wire.Save(w, s.second)
	wire.Save(w, s.third)
}

func (v *system3) StateSave(s state.Sink) {
	v.beforeSave()
	snapshot := state.BeginSnapshot(s, emitThreeChildren)
	s.Capture(&v.v1, &snapshot.first)
	s.Capture(&v.v2, &snapshot.second)
	s.Capture(&v.v3, &snapshot.third)
}

type nameSnapshot struct {
	b, c string
	x, y int64
	z    int32
}

func emitName(w *wire.Writer, s *nameSnapshot) {
	wire.SaveStringField(w, s.b)
	wire.SaveStringField(w, s.c)
	wire.SaveIntField(w, s.x)
	wire.SaveIntField(w, s.y)
	wire.SaveIntField(w, int64(s.z))
}

func (v *multiName) StateSave(s state.Sink) {
	v.beforeSave()
	snapshot := state.BeginSnapshot(s, emitName)
	snapshot.b = v.b
	snapshot.c = v.c
	snapshot.x = v.x
	snapshot.y = v.y
	snapshot.z = v.z
}

func (v *arraySnapshotSource) StateSave(s state.Sink) {
	v.beforeSave()
	snapshot := state.BeginSnapshot(s, emitChild)
	s.Capture(&v.values, &snapshot.child)
}

func (v *arraySnapshotMutator) StateSave(s state.Sink) {
	v.beforeSave()
	snapshot := state.BeginSnapshot(s, emitChild)
	s.Capture(&v.target, &snapshot.child)
}

func (v *arrayTailDiscovery) StateSave(s state.Sink) {
	v.beforeSave()
	snapshot := state.BeginSnapshot(s, emitChild)
	s.Capture(&v.values, &snapshot.child)
}

type emptySnapshot struct{}

func emitEmpty(*wire.Writer, *emptySnapshot) {}

func (v *savableEmptyStruct) StateSave(s state.Sink) {
	v.beforeSave()
	state.BeginSnapshot(s, emitEmpty)
}

// +stateify savable
type conflictingSnapshot struct {
	value int64
	save  func(state.Sink, *int64) `state:"nosave"`
}

func (v *conflictingSnapshot) StateSave(s state.Sink) {
	v.save(s, &v.value)
}
