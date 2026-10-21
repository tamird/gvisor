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

// This diagnostic owner uses the operations that generated StateLoad would
// call. Keep its saved fields identical to the generated representation.
// +stateify type
type mapContainer struct {
	prefix int
	v      map[int]any
}

func (m *mapContainer) StateSave(s state.Sink) {
	s.Save(0, &m.prefix)
	s.Save(1, &m.v)
}

func (m *mapContainer) StateLoad(_ context.Context, s state.Source) {
	key, value := state.MapTypes(m.v)
	state.LoadField(s, 0, &m.prefix, state.IntOps(&m.prefix))
	state.LoadField(s, 1, &m.v, state.MapOps(&m.v, state.IntOps(&key), state.ValueOps(&value)))
}

type namedContainerMap map[int]any

// +stateify type
type mapPtrContainer struct {
	v     *map[int]any
	alias namedContainerMap
	owner *mapContainer
}

func (m *mapPtrContainer) StateSave(s state.Sink) {
	s.Save(0, &m.v)
	s.Save(1, &m.alias)
	s.Save(2, &m.owner)
}

func (m *mapPtrContainer) StateLoad(_ context.Context, s state.Source) {
	var value map[int]any
	key, element := state.MapTypes(value)
	state.LoadField(s, 0, &m.v, state.PointerOps(&m.v, state.MapOps(&value, state.IntOps(&key), state.ValueOps(&element))))
	state.LoadField(s, 1, &m.alias, state.MapOps(&m.alias, state.IntOps(&key), state.ValueOps(&element)))
	s.Load(2, &m.owner)
}

// +stateify savable
type registeredMapStruct struct{}

// +stateify savable
type mapLoadHook struct {
	value int
}

type mapLoadHookContextKey struct{}

func (v *mapLoadHook) afterLoad(ctx context.Context) {
	ctx.Value(mapLoadHookContextKey{}).(func(*mapLoadHook))(v)
}

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

// +stateify type
type countedMapWordContainer struct {
	dynamic any
	values  map[countedMapWord]countedMapWord
	zeros   map[registeredMapStruct]registeredMapStruct
}

func (m *countedMapWordContainer) StateSave(s state.Sink) {
	s.Save(0, &m.dynamic)
	s.Save(1, &m.values)
	s.Save(2, &m.zeros)
}

func (m *countedMapWordContainer) StateLoad(_ context.Context, s state.Source) {
	s.Load(0, &m.dynamic)
	key, value := state.MapTypes(m.values)
	state.LoadField(s, 1, &m.values, state.MapOps(&m.values, state.ValueOps(&key), state.ValueOps(&value)))
	zeroKey, zeroValue := state.MapTypes(m.zeros)
	state.LoadField(s, 2, &m.zeros, state.MapOps(&m.zeros, state.ValueOps(&zeroKey), state.ValueOps(&zeroValue)))
}

// +stateify type
type recursiveMap map[string]recursiveMap

// recursiveMapOps is a finite operation for a recursive container. It contains
// no traversal: child values return to the same shared map loop.
type recursiveMapOps[M ~map[K]M, K ~string] struct{}

func (recursiveMapOps[M, K]) Restore(d state.Decoder, target *M) {
	key, _ := state.MapTypes(*target)
	state.MapOps(target, state.StringOps(&key), recursiveMapOps[M, K]{}).Restore(d, target)
}

func recursiveOps[M ~map[K]M, K ~string](_ *M) recursiveMapOps[M, K] {
	return recursiveMapOps[M, K]{}
}

// +stateify type
type recursiveMapContainer struct {
	dynamic any
	values  recursiveMap
}

func (m *recursiveMapContainer) StateSave(s state.Sink) {
	s.Save(0, &m.dynamic)
	s.Save(1, &m.values)
}

func (m *recursiveMapContainer) StateLoad(_ context.Context, s state.Source) {
	s.Load(0, &m.dynamic)
	state.LoadField(s, 1, &m.values, recursiveOps(&m.values))
}

// A manual saver owns custom capture and does not require a saveValues helper.
// +stateify type
type manualMapValue struct {
	values map[int]int `state:".(map[int]int)"`
}

func (m *manualMapValue) StateSave(s state.Sink)                      { s.SaveValue(0, m.values) }
func (m *manualMapValue) StateLoad(_ context.Context, s state.Source) { s.Load(0, &m.values) }
