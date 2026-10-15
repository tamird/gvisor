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
	"reflect"
	"sync"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/wire"
)

func TestLoadHooks(t *testing.T) {
	runTestCases(t, false, "load-hooks", []any{
		// Root object being a struct.
		afterLoadStruct{v: 1},
		valueLoadStruct{v: 1},
		genericContainer{v: &afterLoadStruct{v: 1}},
		genericContainer{v: &valueLoadStruct{v: 1}},
		sliceContainer{v: []any{&afterLoadStruct{v: 1}}},
		sliceContainer{v: []any{&valueLoadStruct{v: 1}}},
		// Root object being a pointer.
		&afterLoadStruct{v: 1},
		&valueLoadStruct{v: 1},
		&genericContainer{v: &afterLoadStruct{v: 1}},
		&genericContainer{v: &valueLoadStruct{v: 1}},
		&sliceContainer{v: []any{&afterLoadStruct{v: 1}}},
		&sliceContainer{v: []any{&valueLoadStruct{v: 1}}},
		&mapContainer{v: map[int]any{0: &afterLoadStruct{v: 1}}},
		&mapContainer{v: map[int]any{0: &valueLoadStruct{v: 1}}},
	})
}

// TestGeneratedLegacyFields checks accepted raw wire forms through generated
// fields, including forms that state.Save does not produce.
func TestGeneratedLegacyFields(t *testing.T) {
	emptySchema := wire.Struct{TypeID: 2}
	emptySchema.Alloc(0)
	// Arrays omit element tags after the first. Uniform interface elements
	// carry the different no-op and store payloads without changing that grammar.
	wrapped := func(value wire.Object) wire.Object {
		return &wire.Interface{Type: wire.TypeSpecNil{}, Value: value}
	}
	for _, test := range []struct {
		name    string
		field   int
		encoded wire.Object
		initial restoreLegacyFields
		want    restoreLegacyFields
	}{
		{"nil_interface_in_integer", 0, &wire.Interface{Type: wire.TypeSpecNil{}, Value: wire.Int(17)}, restoreLegacyFields{}, restoreLegacyFields{integer: 17}},
		{"schema_in_integer", 0, &emptySchema, restoreLegacyFields{integer: 23}, restoreLegacyFields{integer: 23}},
		{"empty_array_in_string", 1, &wire.Array{}, restoreLegacyFields{}, restoreLegacyFields{}},
		{"array_noops_in_string", 1, &wire.Array{Contents: []wire.Object{wrapped(wire.Nil{}), wrapped(&wire.Ref{})}}, restoreLegacyFields{text: "ab"}, restoreLegacyFields{text: "ab"}},
		{"array_in_slice", 2, &wire.Array{Contents: []wire.Object{wire.Int(17), wire.Int(29)}}, restoreLegacyFields{slice: []int64{0, 0}}, restoreLegacyFields{slice: []int64{17, 29}}},
		{"empty_map_in_slice", 2, &wire.Map{}, restoreLegacyFields{slice: []int64{23}}, restoreLegacyFields{slice: []int64{23}}},
		{"empty_map_in_pointer", 3, &wire.Map{}, restoreLegacyFields{pointer: new(int64(23))}, restoreLegacyFields{pointer: new(int64(23))}},
		{"converted_zero_reference", 3, &wire.Ref{Root: 1 << 32}, restoreLegacyFields{pointer: new(int64(23))}, restoreLegacyFields{pointer: new(int64(23))}},
		{"array_in_array", 4, &wire.Array{Contents: []wire.Object{wrapped(wire.Nil{}), wrapped(wire.Int(29))}}, restoreLegacyFields{array: [2]int64{17, 0}}, restoreLegacyFields{array: [2]int64{17, 29}}},
		{"empty_array_in_map", 5, &wire.Array{}, restoreLegacyFields{mapping: map[int]int64{}}, restoreLegacyFields{mapping: map[int]int64{}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			var saved bytes.Buffer
			writer := wire.Writer{Writer: &saved}
			if err := state.WriteHeader(&writer, 1, true); err != nil {
				t.Fatal(err)
			}
			wire.Save(&writer, &wire.Type{Name: (*restoreLegacyFields)(nil).StateTypeName(), Fields: (*restoreLegacyFields)(nil).StateFields()})
			wire.Save(&writer, &wire.Type{Name: "int64"})
			value := wire.Struct{TypeID: 1}
			value.Alloc(6)
			for i := range 6 {
				*value.Field(i) = wire.Nil{}
			}
			*value.Field(test.field) = test.encoded
			wire.Save(&writer, wire.Uint(1))
			wire.Save(&writer, &value)
			if _, err := state.Load(t.Context(), &saved, &test.initial); err != nil {
				t.Fatal(err)
			}
			if got, want := test.initial, test.want; !reflect.DeepEqual(got, want) {
				t.Errorf("Load = %+v, want %+v", got, want)
			}
		})
	}
}

func TestCycles(t *testing.T) {
	// cs is a single object cycle.
	cs := cycleStruct{nil}
	cs.c = &cs

	// cs1 and cs2 are in a two object cycle.
	cs1 := cycleStruct{nil}
	cs2 := cycleStruct{nil}
	cs1.c = &cs2
	cs2.c = &cs1

	runTestCases(t, false, "cycles", []any{
		cs,
		cs1,
	})
}

func TestDeadlock(t *testing.T) {
	// bs is a single object cycle. This does not cause deadlock because an
	// object cannot wait for itself.
	bs := badCycleStruct{nil}
	bs.b = &bs

	runTestCases(t, false, "self", []any{
		&bs,
	})

	// bs2 and bs2 are in a deadlocking cycle.
	bs1 := badCycleStruct{nil}
	bs2 := badCycleStruct{nil}
	bs1.b = &bs2
	bs2.b = &bs1

	runTestCases(t, true, "deadlock", []any{
		&bs1,
	})
}

func TestSourceReplay(t *testing.T) {
	// A nested Source remains valid after its array cursor and graph decode
	// finish. Reversed wire fields independently exercise FieldOrder.
	var buf bytes.Buffer
	w := wire.Writer{Writer: &buf}
	if err := state.WriteHeader(&w, 1, true); err != nil {
		t.Fatal(err)
	}
	wire.Save(&w, &wire.Type{Name: (*replayLoadStruct)(nil).StateTypeName(), Fields: []string{"second", "omitted", "first"}})
	wire.Save(&w, wire.Uint(1))
	text := wire.String("retained field")
	value := wire.Struct{TypeID: 1}
	value.Alloc(3)
	*value.Field(0), *value.Field(1), *value.Field(2) = &text, wire.Int(99), wire.Int(17)
	wire.Save(&w, &wire.Array{Contents: []wire.Object{&value}})
	const trailing = "following non-state data"
	buf.WriteString(trailing)
	var restored [1]replayLoadStruct
	if _, err := state.Load(t.Context(), &buf, &restored); err != nil {
		t.Fatal(err)
	}
	r := &restored[0]
	if got, want := r.first, int64(17); got != want {
		t.Errorf("first = %d, want %d", got, want)
	}
	if got, want := r.repeated, int64(17); got != want {
		t.Errorf("repeated first = %d, want %d", got, want)
	}
	if got, want := r.second, "retained field"; got != want {
		t.Errorf("second = %q, want %q", got, want)
	}
	if got, want := r.after, "retained field"; got != want {
		t.Errorf("after-load replay = %q, want %q", got, want)
	}
	if got, want := r.omitted, int64(0); got != want {
		t.Errorf("unclaimed field = %d, want %d", got, want)
	}
	var replay int64
	r.source.Load(0, &replay)
	if got, want := replay, int64(17); got != want {
		t.Errorf("retained Source replay = %d, want %d", got, want)
	}
	if got, want := buf.String(), trailing; got != want {
		t.Errorf("unconsumed bytes = %q, want %q", got, want)
	}
}

func TestValidateBeforeLoadHook(t *testing.T) {
	var saved bytes.Buffer
	original := replayLoadStruct{first: 17, second: "valid prefix", omitted: 99}
	if _, err := state.Save(t.Context(), &saved, &original); err != nil {
		t.Fatal(err)
	}
	// Truncating the unclaimed final field must prevent the hook entirely,
	// even though both fields it loads precede the invalid bytes.
	data := saved.Bytes()
	var restored replayLoadStruct
	if _, err := state.Load(t.Context(), bytes.NewReader(data[:len(data)-1]), &restored); err == nil {
		t.Fatal("Load accepted a truncated unclaimed field")
	}
	if got, want := restored.calls, 0; got != want {
		t.Errorf("StateLoad calls before validation = %d, want %d", got, want)
	}
}

func TestConcurrentFieldLayouts(t *testing.T) {
	original := sharedLayoutFields{first: 17, second: 29, text: "shared layout"}
	original.next = &original
	var saved bytes.Buffer
	if _, err := state.Save(t.Context(), &saved, &original); err != nil {
		t.Fatal(err)
	}
	// Start with the actual producer's one-object stream. Reorder both the
	// schema and the corresponding payload slots, leaving graph IDs intact.
	normal := bytes.Clone(saved.Bytes())
	reader := wire.Reader{Reader: &saved}
	count, object, err := state.ReadHeader(&reader)
	if got, want := count, uint64(1); err != nil || got != want || !object {
		t.Fatalf("ReadHeader = %d, %t, %v, want %d, true, nil", got, object, err, want)
	}
	var values wire.ValueBuffer
	defer values.Release()
	schemaValue := values.Load(&reader)
	if schemaValue.Kind() != wire.KindType {
		t.Fatal("Save did not emit a type schema")
	}
	schema := schemaValue.Type()
	if got, want := schema.Fields, []string{"first", "second", "text", "next"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("schema fields = %v, want %v", got, want)
	}
	id := values.Load(&reader).Uint()
	if got, want := id, uint64(1); got != want {
		t.Fatalf("root ID = %v, want %v", got, want)
	}
	value := values.Load(&reader)
	if value.Kind() != wire.KindStruct {
		t.Fatal("Save did not emit a struct")
	}
	if got, want := value.Fields(), 4; got != want {
		t.Fatalf("struct fields = %d, want %d", got, want)
	}
	schema.Fields[0], schema.Fields[1] = schema.Fields[1], schema.Fields[0]
	reorderedValue := wire.Struct{TypeID: value.StructType()}
	reorderedValue.Alloc(value.Fields())
	*reorderedValue.Field(0) = wire.Int(value.Field(1).Int())
	*reorderedValue.Field(1) = wire.Int(value.Field(0).Int())
	text := wire.String(value.Field(2).Text())
	*reorderedValue.Field(2) = &text
	ref := value.Field(3).Reference()
	*reorderedValue.Field(3) = &ref
	var reordered bytes.Buffer
	writer := wire.Writer{Writer: &reordered}
	if err := state.WriteHeader(&writer, count, true); err != nil {
		t.Fatal(err)
	}
	wire.Save(&writer, schema)
	wire.Save(&writer, wire.Uint(id))
	wire.Save(&writer, &reorderedValue)

	var restored [8]sharedLayoutFields
	var errors [8]error
	start := make(chan struct{})
	var workers sync.WaitGroup
	for i := range restored {
		workers.Go(func() {
			<-start
			data := normal
			if i%2 != 0 {
				data = reordered.Bytes()
			}
			_, errors[i] = state.Load(t.Context(), bytes.NewReader(data), &restored[i])
		})
	}
	close(start)
	workers.Wait()
	for i := range restored {
		if err := errors[i]; err != nil {
			t.Fatalf("Load %d: %v", i, err)
		}
		want := original
		want.next = &restored[i]
		want.loads = 1
		if got, want := restored[i], want; got != want {
			t.Errorf("Load %d = %+v, want %+v", i, got, want)
		}
	}
}
