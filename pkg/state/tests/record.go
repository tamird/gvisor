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

// This file deliberately shadows a predeclared type name with an import. The
// generator must use the checked field type, not the spelling of its AST.
import (
	"context"
	int64 "time"

	"gvisor.dev/gvisor/pkg/state"
)

type recordSigned int16
type recordUnsigned uint64
type recordSignedAlias = recordSigned

// +stateify savable
type primitiveRecord struct {
	signed        recordSignedAlias
	unsigned      recordUnsigned
	imported      int64.Duration
	text          string
	enabled       bool
	narrow        float32
	wide          float64
	narrowComplex complex64
	wideComplex   complex128
}

// These names would collide with a fixed generated wire import and its first
// alternate. The ordinary renderer accepts both the type and receiver names.
// +stateify savable
type statewire struct{ value recordSigned }

func (statewire1 *statewire) beforeSave() {}

func (stateSnapshotObject *primitiveRecord) beforeSave() {}

type recordBool bool
type recordString string
type recordFloat float64
type recordComplex complex128
type recordSlice []recordSigned
type recordMap map[recordSigned]recordUnsigned

// The receiver must not shadow the state package in generated Save or Load.
// Defined field types exercise checked scalar widths and aggregate
// destinations without spelling component types.
// +stateify savable
type statePackageReceiver struct {
	value    int          `state:".(stateObject)"`
	object   []state.Type `state:".([]state.Type)"`
	enabled  recordBool
	text     recordString
	fraction recordFloat
	complex  recordComplex
	pointer  referenceIntPointer
	slice    recordSlice
	mapping  recordMap
}

// The replacement receiver must not shadow this custom value's type either.
type stateObject int

func (state *statePackageReceiver) beforeSave() {}

func (state *statePackageReceiver) saveValue() stateObject {
	return stateObject(state.value)
}

func (state *statePackageReceiver) loadValue(_ context.Context, value stateObject) {
	state.value = int(value)
}

// Signature types do not share the receiver's body scope. The generated
// LoadValue body must keep the state import visible for this custom type.
// A slice retains its interface element type when SaveValue receives it as any.
func (state *statePackageReceiver) saveObject() []state.Type { return state.object }

func (state *statePackageReceiver) loadObject(_ context.Context, value []state.Type) {
	state.object = value
}

// The forwarding owner deliberately precedes its underlying type's
// registration. Record construction must not depend on init statement order.
// +stateify savable
// +stateify identtype
type forwardedRecord recordParent

// +stateify savable
type recordParent struct {
	value int
	child *recordParent
}

// +stateify savable
type configuredRecord struct {
	value recordModeValue
}

// +stateify type
type composedRecord struct {
	value   int
	before  int
	after   int
	replace bool
}

func (r *composedRecord) StateSave(s state.Sink) {
	s.Save(1, &r.before)
	child := statewire{value: recordSigned(r.value)}
	if r.replace {
		child.value++
	}
	child.StateSave(s)
	if r.replace {
		s.Save(0, &r.value)
	}
	s.Save(2, &r.after)
	s.Save(3, &r.replace)
}

func (r *composedRecord) StateLoad(_ context.Context, s state.Source) {
	s.Load(0, &r.value)
	s.Load(1, &r.before)
	s.Load(2, &r.after)
	s.Load(3, &r.replace)
}
