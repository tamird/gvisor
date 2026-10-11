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

// The receiver is legal in the canonical saver but must not shadow the state
// package when the checked renderer introduces a qualified capture call.
// +stateify savable
type statePackageReceiver struct {
	value int `state:".(stateObject)"`
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
