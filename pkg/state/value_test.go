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

package state

import (
	"bytes"
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"

	"gvisor.dev/gvisor/pkg/state/wire"
)

// These mutually recursive types use flat tables. Their runtime graph is
// acyclic for completion waits and contains shared map and slice storage.
type engineMap map[int64]engineSlice
type engineSlice []engineMap

var engineIntOps valueOperations
var engineMapOps valueOperations
var engineSliceOps valueOperations
var engineBackingOps valueOperations
var engineCallbacksOps valueOperations
var engineCallbackArrayOps valueOperations

func (*engineMap) StateTypeName() string                    { return "state.engineMap" }
func (*engineMap) StateFields() []string                    { return nil }
func (*engineMap) stateValueOperations() *valueOperations   { return &engineMapOps }
func (*engineSlice) StateTypeName() string                  { return "state.engineSlice" }
func (*engineSlice) StateFields() []string                  { return nil }
func (*engineSlice) stateValueOperations() *valueOperations { return &engineSliceOps }

type engineRoot struct {
	graph        engineMap
	dynamic      any
	value        int64
	dynamicFirst bool
	loaded       bool
}

func (*engineRoot) StateTypeName() string { return "state.engineRoot" }
func (*engineRoot) StateFields() []string { return []string{"graph", "dynamic", "value"} }
func (r *engineRoot) StateSave(s Sink) {
	if r.dynamicFirst {
		s.Save(1, &r.dynamic)
	}
	s.internal.save(0, typedHandle(&r.graph, &engineMapOps))
	if !r.dynamicFirst {
		s.Save(1, &r.dynamic)
	}
	s.internal.save(2, typedHandle(&r.value, &engineIntOps))
}
func (r *engineRoot) StateLoad(_ context.Context, s Source) {
	if r.dynamicFirst {
		s.LoadWait(1, &r.dynamic)
	}
	s.internal.load(0, typedHandle(&r.graph, &engineMapOps), true, nil)
	if !r.dynamicFirst {
		s.LoadWait(1, &r.dynamic)
	}
	s.internal.load(2, typedHandle(&r.value, &engineIntOps), false, nil)
	s.AfterLoad(func() { r.loaded = true })
}

type engineInteriorRoot struct {
	child  *engineMap
	values engineSlice
}

func (*engineInteriorRoot) StateTypeName() string { return "state.engineInteriorRoot" }
func (*engineInteriorRoot) StateFields() []string { return []string{"child", "values"} }
func (r *engineInteriorRoot) StateSave(s Sink) {
	s.Save(0, &r.child)
	s.internal.save(1, typedHandle(&r.values, &engineSliceOps))
}
func (r *engineInteriorRoot) StateLoad(_ context.Context, s Source) {
	s.Load(0, &r.child)
	s.internal.load(1, typedHandle(&r.values, &engineSliceOps), true, nil)
}

type engineCallback struct {
	marker int64
	result *int64
}

func (*engineCallback) StateTypeName() string { return "state.engineCallback" }
func (*engineCallback) StateFields() []string { return []string{"marker", "result"} }
func (v *engineCallback) StateSave(s Sink) {
	s.Save(0, &v.marker)
	s.Save(1, &v.result)
}
func (v *engineCallback) StateLoad(_ context.Context, s Source) {
	s.Load(0, &v.marker)
	s.LoadWait(1, &v.result)
	s.AfterLoad(func() { *v.result = v.marker })
}

type engineCallbacks map[int64][1]engineCallback

func (*engineCallbacks) StateTypeName() string { return "state.engineCallbacks" }
func (*engineCallbacks) StateFields() []string { return nil }
func (*engineCallbacks) stateValueOperations() *valueOperations {
	return &engineCallbacksOps
}

func init() {
	engineIntOps = integerOperations[int64]()
	engineMapOps = mapOperations[engineMap](func() *valueOperations { return &engineIntOps }, func() *valueOperations { return &engineSliceOps })
	engineSliceOps = sliceOperations[engineSlice](func() *valueOperations { return &engineMapOps }, func() *valueOperations { return &engineBackingOps })
	engineBackingOps = backingOperations[engineMap](func() *valueOperations { return &engineMapOps })
	engineCallbackArrayOps = backingOperations[engineCallback](func() *valueOperations { return &reflectedOperations[reflect.Struct] })
	engineCallbacksOps = mapOperations[engineCallbacks](func() *valueOperations { return &engineIntOps }, func() *valueOperations { return &engineCallbackArrayOps })
	Register((*engineMap)(nil))
	Register((*engineSlice)(nil))
	Register((*engineRoot)(nil))
	Register((*engineInteriorRoot)(nil))
	Register((*engineCallback)(nil))
	Register((*engineCallbacks)(nil))
}

func TestCommonEngineGraph(t *testing.T) {
	for _, dynamicSave := range []bool{false, true} {
		for _, dynamicLoad := range []bool{false, true} {
			t.Run(map[bool]string{false: "typed", true: "dynamic"}[dynamicSave]+"_then_"+map[bool]string{false: "typed", true: "dynamic"}[dynamicLoad], func(t *testing.T) {
				leaf := engineMap{41: nil}
				shared := engineSlice{leaf, leaf}
				graph := engineMap{7: shared, 9: shared}
				input := engineRoot{graph: graph, dynamic: graph, value: 123, dynamicFirst: dynamicSave}
				var saved bytes.Buffer
				if _, err := Save(t.Context(), &saved, &input); err != nil {
					t.Fatal(err)
				}
				output := engineRoot{dynamicFirst: dynamicLoad}
				if _, err := Load(t.Context(), &saved, &output); err != nil {
					t.Fatal(err)
				}
				if output.value != input.value || !output.loaded || !reflect.DeepEqual(output.graph, graph) || !reflect.DeepEqual(output.dynamic, graph) {
					t.Fatalf("restored graph/value/hook = %#v", output)
				}
				delete(output.graph[7][0], 41)
				if len(output.graph[9][1]) != 0 {
					t.Fatal("shared child map was copied")
				}
				output.graph[7][0] = engineMap{83: nil}
				if _, ok := output.graph[9][0][83]; !ok {
					t.Fatal("shared backing array was copied")
				}
				delete(output.graph, 9)
				if _, ok := output.dynamic.(engineMap)[9]; ok {
					t.Fatal("dynamic map alias was copied")
				}
			})
		}
	}
}

func TestCommonEngineInteriorFirst(t *testing.T) {
	values := engineSlice{{17: nil}, {29: nil}}
	input := engineInteriorRoot{child: &values[1], values: values}
	var saved bytes.Buffer
	if _, err := Save(t.Context(), &saved, &input); err != nil {
		t.Fatal(err)
	}
	var output engineInteriorRoot
	if _, err := Load(t.Context(), &saved, &output); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(output.values, values) {
		t.Fatalf("restored values = %v, want %v", output.values, values)
	}
	if output.child != &output.values[1] {
		t.Fatalf("restored child = %p, want backing element %p", output.child, &output.values[1])
	}
	*output.child = engineMap{43: nil}
	if _, ok := output.values[1][43]; !ok {
		t.Fatal("interior pointer does not update the restored backing array")
	}
}

func TestCommonEngineRetainedMapValues(t *testing.T) {
	var first, second int64
	input := engineCallbacks{3: {{marker: 31, result: &first}}, 5: {{marker: 53, result: &second}}}
	var saved bytes.Buffer
	if _, err := Save(t.Context(), &saved, &input); err != nil {
		t.Fatal(err)
	}
	var output engineCallbacks
	if _, err := Load(t.Context(), &saved, &output); err != nil {
		t.Fatal(err)
	}
	if len(output) != len(input) {
		t.Fatalf("restored map size = %d, want %d", len(output), len(input))
	}
	for key, want := range input {
		got, ok := output[key]
		if !ok || got[0].marker != want[0].marker || got[0].result == nil {
			t.Fatalf("restored map[%d] = %v, want marker %d and a result", key, got, want[0].marker)
		}
		if *got[0].result != want[0].marker {
			t.Fatalf("retained callback for map[%d] wrote %d, want %d", key, *got[0].result, want[0].marker)
		}
	}
	if output[3][0].result == output[5][0].result {
		t.Fatal("distinct callback results share storage")
	}
}

func TestCommonEngineStoragePermissions(t *testing.T) {
	private := struct{ value int64 }{value: 13}
	var wrongType uint64 = 17
	for _, test := range []struct {
		name    string
		object  reflect.Value
		wantErr string
	}{
		{"unaddressable", reflect.ValueOf(int64(11)), "unaddressable value"},
		{"read_only", reflect.ValueOf(&private).Elem().Field(0), "unexported field"},
		{"wrong_type", reflect.ValueOf(&wrongType).Elem(), "incompatible typed storage"},
	} {
		t.Run(test.name, func(t *testing.T) {
			ds := decodeState{}
			err := safely(func() {
				ds.loadValue(nil, handleWithOperations(test.object, &engineIntOps), wire.Int(29))
			})
			if _, ok := errors.AsType[*ErrState](err); !ok || !strings.Contains(err.Error(), test.wantErr) {
				t.Fatalf("Load error = %v, want ErrState containing %q", err, test.wantErr)
			}
		})
	}
	if got, want := private.value, int64(13); got != want {
		t.Fatalf("read-only destination = %d, want %d", got, want)
	}
	if got, want := wrongType, uint64(17); got != want {
		t.Fatalf("wrong-type destination = %d, want %d", got, want)
	}
}

func TestCommonEngineIntegerNarrowing(t *testing.T) {
	type smallInt int8
	ops := integerOperations[smallInt]()
	for _, test := range []struct {
		name    string
		encoded wire.Int
		want    smallInt
		wantErr string
	}{
		{"minimum", -128, -128, ""},
		{"maximum", 127, 127, ""},
		{"underflow", -129, 127, "signed integer truncated from -129 to 127"},
		{"overflow", 128, -128, "signed integer truncated from 128 to -128"},
	} {
		t.Run(test.name, func(t *testing.T) {
			var value smallInt
			handle := typedHandle(&value, &ops)
			ds := decodeState{}
			err := safely(func() { ds.loadValue(nil, handle, test.encoded) })
			if test.wantErr == "" {
				if err != nil {
					t.Fatal(err)
				}
			} else if _, ok := errors.AsType[*ErrState](err); !ok || !strings.Contains(err.Error(), test.wantErr) {
				t.Fatalf("Load error = %v, want ErrState containing %q", err, test.wantErr)
			}
			if value != test.want {
				t.Fatalf("stored value = %d, want %d", value, test.want)
			}
			var encoded wire.Object
			es := encodeState{}
			es.saveValue(handle, encodeDefault, &encoded)
			if got, want := encoded, wire.Int(test.want); got != want {
				t.Fatalf("saved value = %v, want %v", got, want)
			}
		})
	}
}
