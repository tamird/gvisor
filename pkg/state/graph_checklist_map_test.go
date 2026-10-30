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
	"reflect"
	"testing"

	"gvisor.dev/gvisor/pkg/state/wire"
)

func init() { Register((*deferredMapKey)(nil)) }
func checklistRoundTrip[T any](t *testing.T, root T) (T, error) {
	t.Helper()
	registerCycleTestTypes()
	var buf bytes.Buffer
	// Wrap root below object 0, which Load allows other objects to reference
	// without waiting.
	savedRoot := root
	if _, err := saveGraph(t.Context(), &buf, &savedRoot); err != nil {
		t.Fatalf("saveGraph: %v", err)
	}
	wr := wire.Reader{Reader: &buf}
	hdr, err := ReadHeaderMode(&wr)
	if err != nil {
		t.Fatalf("ReadHeaderMode: %v", err)
	}
	if !hdr.Object || !hdr.Graph {
		t.Fatalf("ReadHeaderMode = %+v, want Object=true Graph=true", hdr)
	}
	var loadedRoot T
	_, err = loadGraph(t.Context(), &buf, &loadedRoot, hdr.Length, 0)
	return loadedRoot, err
}

// deferredMapKey sets its saved field only when its load hook runs.
type deferredMapKey struct{ id uint8 }

func (*deferredMapKey) StateTypeName() string { return "state.deferredMapKey" }
func (*deferredMapKey) StateFields() []string { return []string{"id"} }
func (k *deferredMapKey) StateSave(s Sink)    { s.graph.save(0, &k.id) }
func (k *deferredMapKey) StateLoad(_ context.Context, s Source) {
	var id uint8
	s.graph.load(0, &id)
	s.graph.afterLoad(func() { k.id = id })
}

func TestGraphDeferredMapKeys(t *testing.T) {
	want := [2]map[deferredMapKey]string{
		{{1}: "left", {2}: "right"},
		{{3}: "other"},
	}
	got, err := checklistRoundTrip(t, want)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("loaded maps = %v, want %v", got, want)
	}
}

type mapWord struct {
	p *int
}

type mapWordObservation struct{ token, calls int }
type mapWordKey struct{}

func init() { RegisterStruct((*mapWord)(nil)) }

func (*mapWord) StateTypeName() string { return "state.testMapWord" }
func (*mapWord) StateFields() []string { return nil }
func (*mapWord) StateSave(Sink)        {}
func (v *mapWord) StateLoad(_ context.Context, s Source) {
	s.AfterLoadTo(v)
}
func (v *mapWord) StateAfterLoad(ctx context.Context) {
	o := ctx.Value(mapWordKey{}).(*mapWordObservation)
	o.calls++
	v.p = &o.token
}

func TestGraphChecklistMapValue(t *testing.T) {
	old := GraphEngineEnabled()
	EnableGraphEngine(true)
	t.Cleanup(func() { EnableGraphEngine(old) })
	const key = 17
	in := map[uint8]any{key: mapWord{}}
	var buf bytes.Buffer
	if _, err := Save(t.Context(), &buf, &in); err != nil {
		t.Fatal(err)
	}
	var out map[uint8]any
	observed := mapWordObservation{token: 71}
	ctx := context.WithValue(t.Context(), mapWordKey{}, &observed)
	if _, err := Load(ctx, &buf, &out); err != nil {
		t.Fatal(err)
	}
	v, ok := out[key].(mapWord)
	if !ok || v.p != &observed.token || observed.calls != 1 {
		t.Fatalf("calls=%d map word=%+v has type=%t want pointer to token", observed.calls, v, ok)
	}
}
