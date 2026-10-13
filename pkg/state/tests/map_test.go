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
	"fmt"
	"math"
	"reflect"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/wire"
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

	t.Run("interior", func(t *testing.T) {
		// Discover the interior map before its enclosing object.
		owner := &mapContainer{prefix: 11, v: map[int]any{1: "value"}}
		original := mapPtrContainer{v: &owner.v, owner: owner}
		var buf bytes.Buffer
		if _, err := state.Save(t.Context(), &buf, &original); err != nil {
			t.Fatal(err)
		}
		var restored mapPtrContainer
		if _, err := state.Load(t.Context(), &buf, &restored); err != nil {
			t.Fatal(err)
		}
		if restored.owner == nil {
			t.Fatal("missing enclosing map owner")
		}
		if got, want := restored.v, &restored.owner.v; got != want {
			t.Fatalf("interior map address = %p, want %p", got, want)
		}
		if got, want := restored.owner.prefix, 11; got != want {
			t.Errorf("enclosing prefix = %d, want %d", got, want)
		}
		if got, want := (*restored.v)[1], "value"; got != want {
			t.Errorf("restored map entry = %v, want %v", got, want)
		}
	})

	t.Run("assignable", func(t *testing.T) {
		// Save rejects different Go map types sharing one map. Load permits
		// different assignable destinations, so exercise that wire boundary
		// directly: v registers an unnamed map before alias assigns it.
		var buf bytes.Buffer
		w := wire.Writer{Writer: &buf}
		if err := state.WriteHeader(&w, 2, true); err != nil {
			t.Fatal(err)
		}
		wire.Save(&w, &wire.Type{Name: (*mapPtrContainer)(nil).StateTypeName(), Fields: []string{"v", "alias", "owner"}})
		value := wire.Struct{TypeID: 1}
		value.Alloc(3)
		*value.Field(0), *value.Field(1), *value.Field(2) = &wire.Ref{Root: 2}, &wire.Ref{Root: 2}, wire.Nil{}
		wire.Save(&w, wire.Uint(1))
		wire.Save(&w, &value)
		wire.Save(&w, wire.Uint(2))
		wire.Save(&w, &wire.Map{})
		var restored mapPtrContainer
		if _, err := state.Load(t.Context(), &buf, &restored); err != nil {
			t.Fatal(err)
		}
		if restored.v == nil || *restored.v == nil || restored.alias == nil {
			t.Fatal("missing decoded map aliases")
		}
		restored.alias[2] = "shared"
		if got, want := (*restored.v)[2], "shared"; got != want {
			t.Errorf("alias update = %v, want %v", got, want)
		}
	})
}

func TestMapNaNKeys(t *testing.T) {
	// Both a dynamically introduced map type and a generated struct field
	// must preserve NaN key/value pairs.
	type dynamicMap map[float64]int
	for _, native := range []bool{false, true} {
		name := "dynamic"
		if native {
			name = "generated"
		}
		t.Run(name, func(t *testing.T) {
			const nanBits = uint64(0x7ff8000000000001)
			nan := math.Float64frombits(nanBits)
			original := map[float64]int{1: 3}
			original[nan] = 1
			original[nan] = 2
			var root, destination any
			var dynamic dynamicMap
			var generated nanMapContainer
			if native {
				root = &nanMapContainer{values: original}
				destination = &generated
			} else {
				input := dynamicMap(original)
				root = &input
				destination = &dynamic
			}
			var buf bytes.Buffer
			if _, err := state.Save(t.Context(), &buf, root); err != nil {
				t.Fatal(err)
			}
			if _, err := state.Load(t.Context(), &buf, destination); err != nil {
				t.Fatal(err)
			}
			restored := map[float64]int(dynamic)
			if native {
				restored = generated.values
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
		})
	}
}

func TestCollectionCaptureTiming(t *testing.T) {
	original := collectionCaptureTiming{
		addressed: map[int]int{0: 1},
		snapshot:  map[int]int{0: 2},
		slice:     []int{5, 11, 13}[:1],
	}
	original.mutation.owner = &original
	var buf bytes.Buffer
	if _, err := state.Save(t.Context(), &buf, &original); err != nil {
		t.Fatal(err)
	}
	var restored collectionCaptureTiming
	if _, err := state.Load(t.Context(), &buf, &restored); err != nil {
		t.Fatal(err)
	}
	if got, want := restored.addressed[0], 3; got != want {
		t.Errorf("addressed map = %d, want %d", got, want)
	}
	if got, want := restored.snapshot[0], 2; got != want {
		t.Errorf("value map = %d, want %d", got, want)
	}
	if !restored.mutation.cleared {
		t.Error("slice capacity was not cleared before the later hook")
	}
	if got, want := cap(restored.slice), 3; got != want {
		t.Fatalf("slice capacity = %d, want %d", got, want)
	}
	if got, want := len(restored.slice), 1; got != want {
		t.Fatalf("slice length = %d, want %d", got, want)
	}
	if got, want := restored.slice[:3], []int{7, 0, 0}; !reflect.DeepEqual(got, want) {
		t.Errorf("backing array = %v, want %v", got, want)
	}
}

func TestMapEscapingReceiver(t *testing.T) {
	for _, native := range []bool{false, true} {
		name := "dynamic"
		if native {
			name = "generated"
		}
		t.Run(name, func(t *testing.T) {
			saves := 0
			var original any
			if native {
				original = &escapingMapContainer{values: map[int]escapingMapValue{1: {value: 11, saves: &saves}, 2: {value: 11, saves: &saves}}}
			} else {
				// This exact map shape is not a generated field.
				original = map[uint]escapingMapValue{1: {value: 11, saves: &saves}, 2: {value: 11, saves: &saves}}
			}
			var buf bytes.Buffer
			if _, err := state.Save(t.Context(), &buf, &original); err != nil {
				t.Fatal(err)
			}
			// Each copied entry is saved inline, then its escaped receiver is
			// saved once as a graph node. No receiver may be reused for a later entry.
			if got, want := saves, 4; got != want {
				t.Errorf("save hooks = %d, want %d", got, want)
			}
			var restored any
			if _, err := state.Load(t.Context(), &buf, &restored); err != nil {
				t.Fatal(err)
			}
			var values []escapingMapValue
			if native {
				for _, v := range restored.(*escapingMapContainer).values {
					values = append(values, v)
				}
			} else {
				for _, v := range restored.(map[uint]escapingMapValue) {
					values = append(values, v)
				}
			}
			if got, want := len(values), 2; got != want {
				t.Fatalf("map entries = %d, want %d", got, want)
			}
			if values[0].self == values[1].self {
				t.Fatal("different entries share an escaped receiver")
			}
			for _, v := range values {
				if v.self == nil || v.self.value != v.value || v.self.self != v.self {
					t.Errorf("entry %d has incorrect receiver alias: %+v", v.value, v.self)
				}
			}
		})
	}
}

func TestMapEqualWordSnapshots(t *testing.T) {
	for _, dynamicFirst := range []bool{false, true} {
		for _, deferred := range []bool{false, true} {
			t.Run(fmt.Sprintf("dynamic_first=%t/deferred=%t", dynamicFirst, deferred), func(t *testing.T) {
				original := countedMapWordContainer{
					values: map[countedMapWord]countedMapWord{{value: 7}: {value: 7}, {value: 8}: {value: 8}},
					zeros:  map[registeredMapStruct]registeredMapStruct{{}: {}},
				}
				if dynamicFirst {
					original.dynamic = original.values
				}
				var receivers mapReceiverCensus
				ctx := context.WithValue(t.Context(), mapReceiverKey{}, &receivers)
				var buf bytes.Buffer
				if _, err := state.Save(ctx, &buf, &original); err != nil {
					t.Fatal(err)
				}
				if deferred {
					// Put all object bodies before root1. The map must keep its
					// first registration's storage/restore owner when deferred data
					// is drained, even if the other field later reaches that map.
					reader := wire.Reader{Reader: &buf}
					count, object, err := state.ReadHeader(&reader)
					if err != nil || !object {
						t.Fatalf("ReadHeader = %d, %t, %v", count, object, err)
					}
					var schemas []wire.Object
					type objectBody struct{ id, value wire.Object }
					var objects []objectBody
					for uint64(len(objects)) < count {
						value := wire.Load(&reader)
						if _, ok := value.(*wire.Type); ok {
							schemas = append(schemas, value)
							continue
						}
						objects = append(objects, objectBody{value, wire.Load(&reader)})
					}
					buf.Reset()
					writer := wire.Writer{Writer: &buf}
					if err := state.WriteHeader(&writer, count, true); err != nil {
						t.Fatal(err)
					}
					for _, schema := range schemas {
						wire.Save(&writer, schema)
					}
					for i := len(objects) - 1; i >= 0; i-- {
						wire.Save(&writer, objects[i].id)
						wire.Save(&writer, objects[i].value)
					}
				}
				var restored countedMapWordContainer
				if _, err := state.Load(ctx, &buf, &restored); err != nil {
					t.Fatal(err)
				}
				if got, want := restored, original; !reflect.DeepEqual(got, want) {
					t.Errorf("restored map = %+v, want %+v", got, want)
				}
				for _, census := range []struct {
					name   string
					values []*countedMapWord
				}{{"Save", receivers.saved}, {"Load", receivers.loaded}} {
					if got, want := len(census.values), 4; got != want {
						t.Fatalf("%s hook receivers = %d, want %d", census.name, got, want)
					}
					seen := make(map[*countedMapWord]bool)
					words := make(map[uint64]int)
					for _, receiver := range census.values {
						if seen[receiver] {
							t.Fatalf("%s reused receiver %p", census.name, receiver)
						}
						seen[receiver] = true
						words[receiver.value]++
					}
					if got, want := words, map[uint64]int{7: 2, 8: 2}; !reflect.DeepEqual(got, want) {
						t.Errorf("%s retained receiver values = %v, want %v", census.name, got, want)
					}
				}
				if dynamicFirst {
					restored.values[countedMapWord{value: 11}] = countedMapWord{value: 13}
					if got, want := restored.dynamic.(map[countedMapWord]countedMapWord)[countedMapWord{value: 11}].value, uint64(13); got != want {
						t.Errorf("map alias value = %d, want %d", got, want)
					}
				}
			})
		}
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
		manualMapValue{values: map[int]int{1: 7}},
	})
}
