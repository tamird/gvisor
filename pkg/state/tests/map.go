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
	"maps"

	"gvisor.dev/gvisor/pkg/state"
)

// +stateify savable
type mapContainer struct {
	v map[int]any
}

// +stateify savable
type mapPtrContainer struct {
	v *map[int]any
}

// +stateify savable
type registeredMapStruct struct{}

// +stateify type
type mapLoadHook struct {
	value int
}

type mapLoadHookContextKey struct{}

func (v *mapLoadHook) StateSave(_ context.Context, sink state.Sink) {
	sink.Save(0, &v.value)
}

func (v *mapLoadHook) StateLoad(ctx context.Context, src state.Source) {
	var value int
	src.Load(0, &value)
	src.AfterLoad(func() {
		v.value = value
		ctx.Value(mapLoadHookContextKey{}).(func(*mapLoadHook))(v)
	})
}

// +stateify savable
type mapLoadSnapshot struct {
	entries map[mapLoadHook]int `state:".(map[mapLoadHook]int)"`
}

func (s *mapLoadSnapshot) saveEntries() map[mapLoadHook]int { return s.entries }

func (s *mapLoadSnapshot) loadEntries(_ context.Context, entries map[mapLoadHook]int) {
	s.entries = maps.Clone(entries)
}
