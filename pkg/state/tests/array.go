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
	"bytes"
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

// +stateify savable
type countedArrayContainer struct {
	first    any
	values   []countedMapWord
	pointers []*countedMapWord
	zeros    []registeredMapStruct
}

// Initializers run before generated registration. The known array destination
// therefore has no typed operation yet. Zero references are valid no-ops even
// for struct elements; the later test repeats this after registration.
var earlyArrayValues, earlyArrayError = loadZeroReferenceArray()

func loadZeroReferenceArray() ([2]countedMapWord, error) {
	var encoded bytes.Buffer
	if _, err := state.Save(context.Background(), &encoded, &[2]*int{}); err != nil {
		return [2]countedMapWord{}, err
	}
	values := [2]countedMapWord{{value: 7}, {value: 11}}
	_, err := state.Load(context.Background(), &encoded, &values)
	return values, err
}

// +stateify type
type arraySliceLoadWord struct{ value uint64 }

func (v *arraySliceLoadWord) StateSave(s state.Sink) { s.Save(0, &v.value) }

func (v *arraySliceLoadWord) StateLoad(ctx context.Context, s state.Source) {
	s.Load(0, &v.value)
	load := ctx.Value(arraySliceLoadKey{}).(*arraySliceLoadState)
	load.receivers = append(load.receivers, v)
	if len(load.receivers) == 1 {
		*load.target = load.replacement
	}
}

type arraySliceLoadKey struct{}

type arraySliceLoadState struct {
	target      *[]arraySliceLoadWord
	replacement []arraySliceLoadWord
	receivers   []*arraySliceLoadWord
}

// A handwritten saver can supply an Array to a slice field. Use the public
// producer so the fixture cannot invent invalid wire constructors or
// heterogeneous array framing.
// +stateify type
type arraySliceLoad struct {
	ignored uint64 `state:"nosave"`
	values  []arraySliceLoadWord
}

func (v *arraySliceLoad) StateSave(s state.Sink) {
	var values [2]arraySliceLoadWord
	copy(values[:], v.values)
	s.SaveValue(0, values)
}

func (v *arraySliceLoad) StateLoad(_ context.Context, s state.Source) {
	s.Load(0, &v.values)
}

// +stateify savable
type earlyLoadFields struct {
	ignored uint64 `state:"nosave"`
	value   uint64
	_       uint64 `state:"nosave"`
	text    string
}

// The normal build permits unregistered concrete Save/Load before generated
// init. Race mode deliberately rejects the missing registration; the test
// preserves that distinction instead of attributing a new early load to it.
var earlyFieldsValue, earlyFieldsError = loadEarlyFields()

func loadEarlyFields() (earlyLoadFields, error) {
	original := earlyLoadFields{value: 29, text: "before registration"}
	var encoded bytes.Buffer
	if _, err := state.Save(context.Background(), &encoded, &original); err != nil {
		return earlyLoadFields{}, err
	}
	var loaded earlyLoadFields
	_, err := state.Load(context.Background(), &encoded, &loaded)
	return loaded, err
}
