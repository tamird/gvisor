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
type arrayContainer struct {
	v [1]any
}

// +stateify savable
type arrayPtrContainer struct {
	v *[1]any
}

// +stateify savable
type sliceContainer struct {
	v []any
}

// +stateify savable
type slicePtrContainer struct {
	v *[]any
}

// +stateify type
type arraySigned int16

// +stateify type
type arrayUnsigned uint32

// +stateify type
type arrayString string

// +stateify savable
type arraySnapshotSource struct {
	values [2]uint64
}

// +stateify savable
type arraySnapshotMutator struct {
	target *arraySnapshotSource
}

func (m *arraySnapshotMutator) beforeSave() {
	m.target.values[0] = 99
}

// +stateify savable
type arrayTailDiscovery struct {
	values [][2]uint64
}

// arrayFloatEncoding compares the two existing public save boundaries without
// copying their element conversion logic into a test oracle.
// +stateify type
type arrayFloatEncoding struct {
	floats    [4]float32
	complexes [2]complex64
	byValue   bool `state:"nosave"`
}

func (a *arrayFloatEncoding) StateSave(s state.Sink) {
	if a.byValue {
		s.SaveValue(0, a.floats)
		s.SaveValue(1, a.complexes)
	} else {
		s.Save(0, &a.floats)
		s.Save(1, &a.complexes)
	}
}

func (a *arrayFloatEncoding) StateLoad(_ context.Context, s state.Source) {
	s.Load(0, &a.floats)
	s.Load(1, &a.complexes)
}
