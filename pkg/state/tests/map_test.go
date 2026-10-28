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
	"math"
	"reflect"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
)

var allMapPrimitives = []any{
	bool(true),
	int(1),
	int8(1),
	int16(1),
	int32(1),
	int64(1),
	uint(1),
	uintptr(1),
	uint8(1),
	uint16(1),
	uint32(1),
	uint64(1),
	string(""),
	registeredMapStruct{},
}

var allMapKeys = flatten(allMapPrimitives, pointersTo(allMapPrimitives))

var allMapValues = flatten(allMapPrimitives, pointersTo(allMapPrimitives), interfacesTo(allMapPrimitives))

var emptyMaps = combine(allMapKeys, allMapValues, func(v1, v2 any) any {
	m := reflect.MakeMap(reflect.MapOf(reflect.TypeOf(v1), reflect.TypeOf(v2)))
	return m.Interface()
})

var fullMaps = combine(allMapKeys, allMapValues, func(v1, v2 any) any {
	m := reflect.MakeMap(reflect.MapOf(reflect.TypeOf(v1), reflect.TypeOf(v2)))
	m.SetMapIndex(reflect.Zero(reflect.TypeOf(v1)), reflect.Zero(reflect.TypeOf(v2)))
	return m.Interface()
})

func TestMapAliasing(t *testing.T) {
	v := make(map[int]int)
	ptrToV := &v
	aliases := []map[int]int{v, v}
	runTestCases(t, false, "", []any{ptrToV, aliases})
}

func TestMapAliasingAcrossTypes(t *testing.T) {
	m := map[int]int{1: 7}
	original := mapViews{plain: m, named: namedMap(m), distinct: namedMap{1: 7}}
	var buf bytes.Buffer
	if _, err := state.Save(t.Context(), &buf, &original); err != nil {
		t.Fatalf("Save: %v", err)
	}
	var restored mapViews
	if _, err := state.Load(t.Context(), &buf, &restored); err != nil {
		t.Fatalf("Load: %v", err)
	}
	restored.named[2] = 8
	if got, want := restored.plain[2], 8; got != want {
		t.Errorf("plain[2] after writing named[2] = %d, want %d", got, want)
	}
	delete(restored.plain, 1)
	if _, ok := restored.named[1]; ok {
		t.Error("named[1] still present after deleting plain[1]")
	}
	if got, want := restored.distinct, (namedMap{1: 7}); !reflect.DeepEqual(got, want) {
		t.Errorf("distinct = %v, want %v", got, want)
	}
}

func TestMapScalarEntries(t *testing.T) {
	type key int
	type value string
	runTestCases(t, false, "", []any{
		map[key]value{-1: "negative", 0: "", 1: "positive"},
		map[complex128]complex128{0: 0, 1 + 2i: 3 + 4i, -1 - 2i: -3 - 4i},
		mapContainer{v: map[int]any{0: nil, 1: inner{v: 1}, 2: &inner{v: 2}}},
		map[inner]int{{v: 1}: 10, {v: 2}: 20},
	})
}

func TestMapNaNKeys(t *testing.T) {
	// Identical NaNs compare unequal, so both entries must survive even
	// though neither can be retrieved using a map lookup.
	const nanBits = uint64(0x7ff8000000000001)
	nan := math.Float64frombits(nanBits)
	original := map[float64]int{1: 3}
	original[nan] = 1
	original[nan] = 2
	var buf bytes.Buffer
	if _, err := state.Save(t.Context(), &buf, &original); err != nil {
		t.Fatalf("Save failed: %v", err)
	}
	var restored map[float64]int
	if _, err := state.Load(t.Context(), &buf, &restored); err != nil {
		t.Fatalf("Load failed: %v", err)
	}
	want := map[int]uint64{1: nanBits, 2: nanBits, 3: math.Float64bits(1)}
	if len(restored) != len(want) {
		t.Fatalf("Restored %d entries, want %d", len(restored), len(want))
	}
	for key, value := range restored {
		if bits, ok := want[value]; !ok || math.Float64bits(key) != bits {
			t.Fatalf("Unexpected restored entry: key bits=%#x, value=%d", math.Float64bits(key), value)
		}
		delete(want, value)
	}
}

func TestMapsEmpty(t *testing.T) {
	runTestCases(t, false, "plain", emptyMaps)
	runTestCases(t, false, "pointers", pointersTo(emptyMaps))
	runTestCases(t, false, "interfaces", interfacesTo(emptyMaps))
	runTestCases(t, false, "interfacesToPointers", interfacesTo(pointersTo(emptyMaps)))
}

func TestMapsFull(t *testing.T) {
	runTestCases(t, false, "plain", fullMaps)
	runTestCases(t, false, "pointers", pointersTo(fullMaps))
	runTestCases(t, false, "interfaces", interfacesTo(fullMaps))
	runTestCases(t, false, "interfacesToPointer", interfacesTo(pointersTo(fullMaps)))
}

func TestMapContainers(t *testing.T) {
	var (
		nilMap   map[int]any
		emptyMap = make(map[int]any)
		fullMap  = map[int]any{0: nil}
	)
	runTestCases(t, false, "", []any{
		mapContainer{v: nilMap},
		mapContainer{v: emptyMap},
		mapContainer{v: fullMap},
		mapPtrContainer{v: nil},
		mapPtrContainer{v: &nilMap},
		mapPtrContainer{v: &emptyMap},
		mapPtrContainer{v: &fullMap},
	})
}

func TestMapNilValues(t *testing.T) {
	seven := 7
	runTestCases(t, false, "", []any{
		map[int]*int{1: &seven, 2: nil},
		map[int]map[int]int{1: {7: 9}, 2: nil},
		map[int][]int{1: {7}, 2: nil, 3: {}},
		map[int]any{1: &seven, 2: (*int)(nil), 3: nil},
	})
}

func TestMapNilKeys(t *testing.T) {
	t.Run("pointer", func(t *testing.T) {
		seven := 7
		original := map[*int]int{&seven: 1, nil: 2}
		var buf bytes.Buffer
		if _, err := state.Save(t.Context(), &buf, &original); err != nil {
			t.Fatalf("Save: %v", err)
		}
		var loaded map[*int]int
		if _, err := state.Load(t.Context(), &buf, &loaded); err != nil {
			t.Fatalf("Load: %v", err)
		}
		if got, want := len(loaded), 2; got != want {
			t.Fatalf("map length = %d, want %d", got, want)
		}
		if got, want := loaded[nil], 2; got != want {
			t.Errorf("nil key value = %d, want %d", got, want)
		}
		for key, value := range loaded {
			if key != nil && (*key != 7 || value != 1) {
				t.Errorf("non-nil key entry = %d: %d, want 7: 1", *key, value)
			}
		}
	})
	runTestCases(t, false, "interface", []any{
		map[any]int{7: 1, (*int)(nil): 2, nil: 3},
	})
}

func TestMapLoadHookReceivers(t *testing.T) {
	for _, test := range []struct {
		name  string
		value any
	}{
		{"struct keys", map[mapLoadHook]int{{1}: 1, {2}: 2}},
		{"struct values", map[int]mapLoadHook{1: {1}, 2: {2}}},
		{"array keys", map[[1]mapLoadHook]int{{{1}}: 1, {{2}}: 2}},
		{"array values", map[int][1]mapLoadHook{1: {{1}}, 2: {{2}}}},
		{"interface keys", map[any]int{mapLoadHook{1}: 1, mapLoadHook{2}: 2}},
		{"interface values", map[int]any{1: mapLoadHook{1}, 2: mapLoadHook{2}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			// Both hooks run after the map entries have been decoded. If
			// aggregate storage is reused, both callbacks see the last entry.
			receivers := make(map[*mapLoadHook]struct{})
			values := make(map[int]int)
			ctx := context.WithValue(t.Context(), mapLoadHookContextKey{}, func(v *mapLoadHook) {
				receivers[v] = struct{}{}
				values[v.value]++
			})
			original := reflect.New(reflect.TypeOf(test.value))
			original.Elem().Set(reflect.ValueOf(test.value))
			var buf bytes.Buffer
			if _, err := state.Save(ctx, &buf, original.Interface()); err != nil {
				t.Fatalf("Save: %v", err)
			}
			loaded := reflect.New(original.Elem().Type())
			if _, err := state.Load(ctx, &buf, loaded.Interface()); err != nil {
				t.Fatalf("Load: %v", err)
			}
			if got, want := loaded.Elem().Interface(), test.value; !reflect.DeepEqual(got, want) {
				t.Errorf("loaded map = %#v, want %#v", got, want)
			}
			if got, want := len(receivers), 2; got != want {
				t.Errorf("distinct hook receivers = %d, want %d", got, want)
			}
			if got, want := values, (map[int]int{1: 1, 2: 1}); !reflect.DeepEqual(got, want) {
				t.Errorf("hook values = %v, want %v", got, want)
			}
		})
	}
}
