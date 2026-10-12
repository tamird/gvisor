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
type mapContainer struct {
	prefix int
	v      map[int]any
}

type namedContainerMap map[int]any

// +stateify savable
type mapPtrContainer struct {
	v     *map[int]any
	alias namedContainerMap
	owner *mapContainer
}

// +stateify savable
type registeredMapStruct struct{}

// +stateify savable
type collectionCaptureTiming struct {
	addressed map[int]int
	snapshot  map[int]int `state:".(map[int]int)"`
	slice     []int
	mutation  collectionMutation
}

func (c *collectionCaptureTiming) saveSnapshot() map[int]int                     { return c.snapshot }
func (c *collectionCaptureTiming) loadSnapshot(_ context.Context, m map[int]int) { c.snapshot = m }

// +stateify savable
type collectionMutation struct {
	owner   *collectionCaptureTiming `state:"nosave"`
	cleared bool
}

func (m *collectionMutation) beforeSave() {
	if m.owner == nil {
		return
	}
	c := m.owner
	tail := c.slice[:cap(c.slice)]
	m.cleared = tail[1] == 0 && tail[2] == 0
	c.addressed = map[int]int{0: 3}
	c.snapshot = map[int]int{0: 4}
	tail[0] = 7
	c.slice = []int{9}
}

// +stateify savable
type escapingMapValue struct {
	value int
	self  *escapingMapValue
	saves *int `state:"nosave"`
}

func (v *escapingMapValue) beforeSave() {
	v.self = v
	if v.saves != nil {
		*v.saves++
	}
}

// +stateify savable
type escapingMapContainer struct {
	values map[int]escapingMapValue
}

// This ordinary saved field gives the NaN case a native map owner. Other
// dynamically constructed map shapes in map_test.go retain adapter coverage.
// +stateify savable
type nanMapContainer struct {
	values map[float64]int
}

type mapReceiverKey struct{}

type mapReceiverCensus struct {
	saved  []*countedMapWord
	loaded []*countedMapWord
}

// One-word equal values must retain MapRange's separate indirect snapshot
// keys; plain interface boxing is permitted to reuse equal small values.
// +stateify type
type countedMapWord struct{ value uint64 }

func (v *countedMapWord) StateSave(s state.Sink) {
	receivers := s.Context().Value(mapReceiverKey{}).(*mapReceiverCensus)
	receivers.saved = append(receivers.saved, v)
	s.Save(0, &v.value)
}

func (v *countedMapWord) StateLoad(ctx context.Context, s state.Source) {
	receivers := ctx.Value(mapReceiverKey{}).(*mapReceiverCensus)
	receivers.loaded = append(receivers.loaded, v)
	s.Load(0, &v.value)
}

// +stateify savable
type countedMapWordContainer struct {
	values map[countedMapWord]countedMapWord
	zeros  map[registeredMapStruct]registeredMapStruct
}

// A manual saver owns custom capture and does not require a saveValues helper.
// +stateify type
type manualMapValue struct {
	values map[int]int `state:".(map[int]int)"`
}

func (m *manualMapValue) StateSave(s state.Sink)                      { s.SaveValue(0, m.values) }
func (m *manualMapValue) StateLoad(_ context.Context, s state.Source) { s.Load(0, &m.values) }
