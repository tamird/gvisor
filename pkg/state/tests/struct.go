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
	"time"

	"gvisor.dev/gvisor/pkg/state"
)

// +stateify savable
type timeContainer struct {
	timestamp time.Time
	pointer   *time.Time
}

type unregisteredEmptyStruct struct{}

// typeOnlyEmptyStruct just implements the state.Type interface.
type typeOnlyEmptyStruct struct{}

func (*typeOnlyEmptyStruct) StateTypeName() string { return "registeredEmptyStruct" }

func (*typeOnlyEmptyStruct) StateFields() []string { return nil }

// +stateify savable
type savableEmptyStruct struct{}

// +stateify savable
type emptyStructPointer struct {
	nothing *struct{}
}

// +stateify savable
type outerSame struct {
	inner inner
}

// +stateify savable
type outerFieldFirst struct {
	inner inner
	v     int64
}

// +stateify savable
type outerFieldSecond struct {
	v     int64
	inner inner
}

// +stateify savable
type outerArray struct {
	inner [2]inner
}

// +stateify savable
type outerSlice struct {
	inner []inner
}

// +stateify savable
type inner struct {
	v int64
}

// +stateify savable
type outerFieldValue struct {
	inner innerFieldValue
}

// +stateify savable
type innerFieldValue struct {
	v int64 `state:".(*savedFieldValue)"`
}

// +stateify savable
type savedFieldValue struct {
	v int64
}

func (ifv *innerFieldValue) saveV() *savedFieldValue {
	return &savedFieldValue{ifv.v}
}

func (ifv *innerFieldValue) loadV(_ context.Context, sfv *savedFieldValue) {
	ifv.v = sfv.v
}

// +stateify savable
type system struct {
	v1 any
	v2 any
}

// +stateify savable
type system3 struct {
	v1 any
	v2 any
	v3 any
}

// +stateify savable
type multiName struct {
	_, b, c string
	x, y    int64
	z       int32
}

// snapshotEmbedded requires the parent's saver to include its extra field.
// +stateify savable
type snapshotEmbedded struct {
	inner
	parent int64
}

// +stateify savable
type snapshotPointerEmbedded struct {
	*inner
}

// snapshotNilEmbedded tests a promoted method through a nil embedding chain.
// +stateify savable
type snapshotNilEmbedded struct {
	*snapshotPointerEmbedded
	parent int64
}

// +stateify savable
type countedSave struct {
	value  int64
	parent *countedSaveParent
	calls  *int `state:"nosave"`
}

func (v *countedSave) beforeSave() {
	*v.calls++
}

// +stateify savable
type countedSaveParent struct {
	child countedSave
	value int64
}

// manualForwardedParent captures an external receiver into matching slots.
// Its child may already have a resolver node even though the receiver does not.
// +stateify type
type manualForwardedParent struct {
	child  countedSave
	value  int64
	source *countedSaveParent `state:"nosave"`
}

func (m *manualForwardedParent) StateSave(s state.Sink) {
	m.source.StateSave(s)
}

func (m *manualForwardedParent) StateLoad(_ context.Context, s state.Source) {
	s.Load(0, &m.child)
	s.Load(1, &m.value)
}

// +stateify savable
type countedSaveGrandparent struct {
	parent countedSaveParent
}

// manualInlineSave saves an external object inline, before or after another
// saver reaches that same object through a pointer.
// +stateify type
type manualInlineSave struct {
	value *countedSave
}

func (m *manualInlineSave) StateSave(s state.Sink) {
	s.Save(0, m.value)
}

func (m *manualInlineSave) StateLoad(_ context.Context, s state.Source) {
	m.value = new(countedSave)
	s.Load(0, m.value)
}

type zeroSaveCountKey struct{}

// +stateify type
type countedZeroSave struct{}

func (*countedZeroSave) StateSave(s state.Sink) {
	*s.Context().Value(zeroSaveCountKey{}).(*int)++
}

func (*countedZeroSave) StateLoad(context.Context, state.Source) {}
