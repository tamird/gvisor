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
	"fmt"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"

	"gvisor.dev/gvisor/pkg/state/wire"
)

type cycleTestNode struct {
	waitFor *cycleTestNode
}

func (*cycleTestNode) StateTypeName() string { return "state.cycleTestNode" }
func (*cycleTestNode) StateFields() []string { return []string{"waitFor"} }
func (n *cycleTestNode) StateSave(s Sink)    { s.graph.save(0, &n.waitFor) }
func (n *cycleTestNode) StateLoad(_ context.Context, s Source) {
	s.graph.loadWait(0, &n.waitFor)
	s.graph.afterLoad(func() {})
}

type cycleStemNode struct {
	waitFor *cycleTestNode
}

func (*cycleStemNode) StateTypeName() string { return "state.cycleStemNode" }
func (*cycleStemNode) StateFields() []string { return []string{"waitFor"} }
func (n *cycleStemNode) StateSave(s Sink)    { s.graph.save(0, &n.waitFor) }
func (n *cycleStemNode) StateLoad(_ context.Context, s Source) {
	s.graph.loadWait(0, &n.waitFor)
	s.graph.afterLoad(func() {})
}

type cycleCollectionNode struct {
	s []*cycleCollectionNode
	m map[int]*cycleCollectionNode
}

func (*cycleCollectionNode) StateTypeName() string { return "state.cycleCollectionNode" }
func (*cycleCollectionNode) StateFields() []string { return []string{"s", "m"} }
func (n *cycleCollectionNode) StateSave(s Sink) {
	s.graph.save(0, &n.s)
	s.graph.save(1, &n.m)
}
func (n *cycleCollectionNode) StateLoad(_ context.Context, s Source) {
	s.graph.loadWait(0, &n.s)
	s.graph.loadWait(1, &n.m)
	s.graph.afterLoad(func() {})
}

var registerCycleTestTypes = sync.OnceFunc(func() {
	Register((*cycleTestNode)(nil))
	Register((*cycleStemNode)(nil))
	Register((*cycleCollectionNode)(nil))
	Register((*cbOrderNode)(nil))
	fingerprint.Store(0)
})

func roundTripGraphErr[T any](t *testing.T, root T) error {
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
	return err
}

func TestGraphDependencyCycle(t *testing.T) {
	t.Run("direct_two_node_cycle", func(t *testing.T) {
		a := &cycleTestNode{}
		b := &cycleTestNode{waitFor: a}
		a.waitFor = b

		err := roundTripGraphErr(t, a)
		const want = `incomplete graph: dependency cycle: {"state.cycleTestNode" => "state.cycleTestNode" => "state.cycleTestNode"}`
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Fatalf("loadGraph = %v, want substring %s", err, want)
		}
	})

	t.Run("stem_outside_cycle_is_trimmed", func(t *testing.T) {
		a := &cycleTestNode{}
		b := &cycleTestNode{waitFor: a}
		a.waitFor = b
		stem := &cycleStemNode{waitFor: a}

		err := roundTripGraphErr(t, stem)
		const want = `incomplete graph: dependency cycle: {"state.cycleTestNode" => "state.cycleTestNode" => "state.cycleTestNode"}`
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Fatalf("loadGraph = %v, want substring %s", err, want)
		}
		if strings.Contains(err.Error(), "cycleStemNode") {
			t.Fatalf("cycle diagnostic included non-cycle stem node: %v", err)
		}
	})

	t.Run("array_cycle_formats_array_kind", func(t *testing.T) {
		n := &cycleCollectionNode{}
		n.s = []*cycleCollectionNode{n}

		err := roundTripGraphErr(t, n)
		if err == nil || !strings.Contains(err.Error(), "incomplete graph: dependency cycle:") || !strings.Contains(err.Error(), `"[1]*state.cycleCollectionNode"`) {
			t.Fatalf("loadGraph = %v, want dependency cycle naming [1]*state.cycleCollectionNode", err)
		}
	})

	t.Run("map_cycle_formats_map_kind", func(t *testing.T) {
		n := &cycleCollectionNode{}
		n.m = map[int]*cycleCollectionNode{0: n}

		err := roundTripGraphErr(t, n)
		if err == nil || !strings.Contains(err.Error(), "incomplete graph: dependency cycle:") || !strings.Contains(err.Error(), `"map[int]*state.cycleCollectionNode"`) {
			t.Fatalf("loadGraph = %v, want dependency cycle naming map[int]*state.cycleCollectionNode", err)
		}
	})
}

type cbOrderLogKey struct{}

type cbOrderNode struct {
	name      string
	early     []*cbOrderNode
	waitFor   []*cbOrderNode
	valueRef  *cbOrderNode
	scalarVal int64

	// Non-saved fields populated during Load callbacks.
	scalarDone    bool
	valueRefDone  bool
	afterStep1    bool
	afterLoadDone bool
	computed      int64
}

func (*cbOrderNode) StateTypeName() string { return "state.cbOrderNode" }
func (*cbOrderNode) StateFields() []string {
	return []string{"name", "early", "waitFor", "scalarVal", "valueRef"}
}

func (n *cbOrderNode) StateSave(s Sink) {
	s.graph.save(0, &n.name)
	s.graph.save(1, &n.early)
	s.graph.save(2, &n.waitFor)
	s.graph.saveValue(3, n.scalarVal)
	s.graph.saveValue(4, n.valueRef)
}

func (n *cbOrderNode) StateLoad(ctx context.Context, s Source) {
	record := func(step string) {
		if logPtr, ok := ctx.Value(cbOrderLogKey{}).(*[]string); ok {
			*logPtr = append(*logPtr, fmt.Sprintf("%s:%s", n.name, step))
		}
	}
	s.graph.load(0, &n.name)
	s.graph.load(1, &n.early)
	s.graph.loadWait(2, &n.waitFor)
	s.graph.loadValue(3, new(int64), func(v any) {
		if n.valueRefDone || n.afterStep1 || n.afterLoadDone {
			Failf("%s: scalar loadValue ran out of order", n.name)
		}
		n.scalarVal = v.(int64)
		n.scalarDone = true
		record("scalar")
	})
	s.graph.loadValueTo(4, (**cbOrderNode)(nil), n)
	s.graph.afterLoad(func() {
		if !n.scalarDone || !n.valueRefDone || n.afterLoadDone {
			Failf("%s: first afterLoad ran out of order (scalar=%v valueRef=%v after=%v)", n.name, n.scalarDone, n.valueRefDone, n.afterLoadDone)
		}
		n.afterStep1 = true
		record("after1")
	})
	s.graph.afterLoadTo(n)
}

func (n *cbOrderNode) StateLoadValue(ctx context.Context, slot int, v any) {
	if slot != 4 {
		Failf("%s: unexpected slot %d", n.name, slot)
	}
	if !n.scalarDone || n.afterStep1 || n.afterLoadDone {
		Failf("%s: StateLoadValue ran out of order", n.name)
	}
	ref := v.(*cbOrderNode)
	if ref != nil && !ref.afterLoadDone {
		Failf("%s: valueRef %s had not completed AfterLoad", n.name, ref.name)
	}
	n.valueRef = ref
	n.valueRefDone = true
	if logPtr, ok := ctx.Value(cbOrderLogKey{}).(*[]string); ok {
		*logPtr = append(*logPtr, fmt.Sprintf("%s:valueRef", n.name))
	}
}

func (n *cbOrderNode) StateAfterLoad(ctx context.Context) {
	if !n.scalarDone || !n.valueRefDone || !n.afterStep1 || n.afterLoadDone {
		Failf("%s: AfterLoad ran out of order", n.name)
	}
	sum := n.scalarVal
	for _, dep := range n.waitFor {
		if !dep.afterLoadDone {
			Failf("%s: wait dependency %s had not completed AfterLoad", n.name, dep.name)
		}
		sum += dep.computed
	}
	if n.valueRef != nil {
		sum += n.valueRef.computed * 10
	}
	n.computed = sum
	n.afterLoadDone = true
	if logPtr, ok := ctx.Value(cbOrderLogKey{}).(*[]string); ok {
		*logPtr = append(*logPtr, fmt.Sprintf("%s:after2", n.name))
	}
}

func TestGraphCallbackOrder(t *testing.T) {
	registerCycleTestTypes()

	// Build a diamond + valueRef dependency graph while forcing discovery in
	// reverse topological order (A discovered first, leaf D discovered last):
	//   A waits on B and C, and has a valueRef to E;
	//   B and C both wait on D;
	//   E waits on C.
	d := &cbOrderNode{name: "D", scalarVal: 1}
	b := &cbOrderNode{name: "B", waitFor: []*cbOrderNode{d}, scalarVal: 2}
	c := &cbOrderNode{name: "C", waitFor: []*cbOrderNode{d}, scalarVal: 4}
	e := &cbOrderNode{name: "E", waitFor: []*cbOrderNode{c}, scalarVal: 8}
	a := &cbOrderNode{
		name:      "A",
		early:     []*cbOrderNode{b, c, e, d},
		waitFor:   []*cbOrderNode{b, c},
		valueRef:  e,
		scalarVal: 16,
	}

	var buf bytes.Buffer
	if _, err := saveGraph(t.Context(), &buf, &a); err != nil {
		t.Fatalf("saveGraph: %v", err)
	}
	wr := wire.Reader{Reader: &buf}
	hdr, err := ReadHeaderMode(&wr)
	if err != nil {
		t.Fatalf("ReadHeaderMode: %v", err)
	}

	var events []string
	ctx := context.WithValue(t.Context(), cbOrderLogKey{}, &events)
	var loaded *cbOrderNode
	if _, err := loadGraph(ctx, &buf, &loaded, hdr.Length, 0); err != nil {
		t.Fatalf("loadGraph: %v", err)
	}

	// Verify exact computed state propagation:
	// D = 1; B = 2 + 1 = 3; C = 4 + 1 = 5; E = 8 + 5 = 13;
	// A = 16 + B(3) + C(5) + 10*E(13) = 154.
	if got, want := loaded.computed, int64(154); got != want {
		t.Fatalf("loaded.computed = %d, want %d (events=%v)", got, want, events)
	}

	pos := func(ev string) int {
		idx := slices.Index(events, ev)
		if idx < 0 {
			t.Fatalf("missing callback event %q in %v", ev, events)
		}
		return idx
	}
	if len(events) != 5*4 {
		t.Fatalf("got %d callback events %v, want 20 (4 per node)", len(events), events)
	}

	// Per-node intra-object FIFO order: scalar -> valueRef -> after1 -> after2.
	for _, name := range []string{"A", "B", "C", "D", "E"} {
		s, vr, a1, a2 := pos(name+":scalar"), pos(name+":valueRef"), pos(name+":after1"), pos(name+":after2")
		if !(s < vr && vr < a1 && a1 < a2) {
			t.Errorf("node %s intra-object order violated: scalar=%d valueRef=%d after1=%d after2=%d (%v)", name, s, vr, a1, a2, events)
		}
	}

	// Cross-object topological dependency constraints.
	for _, edge := range [][2]string{
		{"D:after2", "B:after1"},
		{"D:after2", "C:after1"},
		{"C:after2", "E:after1"},
		{"E:after2", "A:valueRef"},
		{"B:after2", "A:after1"},
		{"C:after2", "A:after1"},
	} {
		if pos(edge[0]) >= pos(edge[1]) {
			t.Errorf("dependency order violated: %s (%d) did not run before %s (%d); events=%v", edge[0], pos(edge[0]), edge[1], pos(edge[1]), events)
		}
	}
}

// TestGraphLoadNilRoot checks that Load rejects a nil root cleanly.
func TestGraphLoadNilRoot(t *testing.T) {
	saved := 7
	var buf bytes.Buffer
	if _, err := saveGraph(t.Context(), &buf, &saved); err != nil {
		t.Fatalf("saveGraph: %v", err)
	}
	hdr, err := ReadHeaderMode(&wire.Reader{Reader: &buf})
	if err != nil {
		t.Fatalf("ReadHeaderMode: %v", err)
	}
	for _, root := range []any{nil, (*int)(nil)} {
		data := bytes.NewReader(buf.Bytes())
		_, err := loadGraph(t.Context(), data, root, hdr.Length, 0)
		const want = "Load requires a non-nil pointer"
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("loadGraph(%T(nil)) = %v, want substring %q", root, err, want)
		}
	}
}

// TestGraphDecodeValueIntegerOverflow checks that out-of-range integers for
// narrow integer types are rejected rather than truncated, matching the wire
// engine and the typed loaders.
func TestGraphDecodeValueIntegerOverflow(t *testing.T) {
	stream := func(rootType reflect.Type, enc func(*encBuf)) []byte {
		var tt typeTable
		idx := tt.rootIndex(typeInfoOf(rootType))
		var raw encBuf
		raw.uint32(streamMagic)
		raw.uint64(schemaFingerprint())
		raw.uvarint(uint64(len(tt.types)))
		raw.bytes(tt.buf.b)
		raw.uvarint(1)
		raw.byte(objKindValue)
		raw.uvarint(uint64(idx))
		raw.uvarint(0)
		enc(&raw)
		return raw.b
	}
	signed := func(v int64) func(*encBuf) { return func(b *encBuf) { b.varint(v) } }
	unsigned := func(v uint64) func(*encBuf) { return func(b *encBuf) { b.uvarint(v) } }

	for _, tc := range []struct {
		name string
		root any
		enc  func(*encBuf)
	}{
		{"int16", new(int16), signed(1 << 16)},
		{"int16_negative", new(int16), signed(-1<<15 - 1)},
		{"int32", new(int32), signed(1 << 32)},
		{"uint16", new(uint16), unsigned(1 << 16)},
		{"uint32", new(uint32), unsigned(1 << 32)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rv := reflect.ValueOf(tc.root)
			rv.Elem().SetZero()
			raw := stream(rv.Type().Elem(), tc.enc)
			if _, err := loadGraph(t.Context(), bytes.NewReader(raw), tc.root, uint64(len(raw)), 0); err == nil || !strings.Contains(err.Error(), "overflows") {
				t.Errorf("loadGraph(%s) = %v (root=%v), want overflow error", tc.name, err, rv.Elem())
			}
		})
	}

	// In-range boundary values still load.
	for _, tc := range []struct {
		root any
		want any
		enc  func(*encBuf)
	}{
		{new(int16), int16(-1 << 15), signed(-1 << 15)},
		{new(int32), int32(1<<31 - 1), signed(1<<31 - 1)},
		{new(uint16), uint16(1<<16 - 1), unsigned(1<<16 - 1)},
		{new(uint32), uint32(1<<32 - 1), unsigned(1<<32 - 1)},
	} {
		rv := reflect.ValueOf(tc.root)
		raw := stream(rv.Type().Elem(), tc.enc)
		if _, err := loadGraph(t.Context(), bytes.NewReader(raw), tc.root, uint64(len(raw)), 0); err != nil {
			t.Errorf("loadGraph(%v) = %v, want nil", tc.want, err)
		} else if got := rv.Elem().Interface(); got != tc.want {
			t.Errorf("loadGraph(%T) = %v, want %v", tc.want, got, tc.want)
		}
	}
}

type qaNamedMap map[int]int

type qaMapViews struct {
	plain        map[int]int
	named, other qaNamedMap
}

func (*qaMapViews) StateTypeName() string { return "state.qaMapViews" }
func (*qaMapViews) StateFields() []string { return []string{"plain", "named", "other"} }
func (v *qaMapViews) StateSave(s Sink) {
	s.graph.save(0, &v.plain)
	s.graph.save(1, &v.named)
	s.graph.save(2, &v.other)
}
func (v *qaMapViews) StateLoad(_ context.Context, s Source) {
	s.graph.load(0, &v.plain)
	s.graph.load(1, &v.named)
	s.graph.load(2, &v.other)
}

type qaWord struct{ p *int }
type qaReport struct {
	token, wordCalls, parentCalls, wordsSeenByParent int
	parentSawToken                                   bool
}
type qaContextKey struct{}

func (*qaWord) StateTypeName() string                   { return "state.qaDirectWord" }
func (*qaWord) StateFields() []string                   { return nil }
func (*qaWord) StateSave(Sink)                          {}
func (v *qaWord) StateLoad(_ context.Context, s Source) { s.graph.afterLoadTo(v) }
func (v *qaWord) StateAfterLoad(ctx context.Context) {
	o := ctx.Value(qaContextKey{}).(*qaReport)
	o.wordCalls++
	v.p = &o.token
}

type qaParent struct{ values map[uint8]any }

func (*qaParent) StateTypeName() string { return "state.qaWaitingParent" }
func (*qaParent) StateFields() []string { return []string{"values"} }
func (v *qaParent) StateSave(s Sink)    { s.graph.save(0, &v.values) }
func (v *qaParent) StateLoad(_ context.Context, s Source) {
	s.graph.loadWait(0, &v.values)
	s.graph.afterLoadTo(v)
}
func (v *qaParent) StateAfterLoad(ctx context.Context) {
	o := ctx.Value(qaContextKey{}).(*qaReport)
	o.parentCalls++
	o.wordsSeenByParent = o.wordCalls
	if w, ok := v.values[7].(qaWord); ok {
		o.parentSawToken = w.p == &o.token
	}
}

func qaRoundTrip[T any](t *testing.T, ctx context.Context, in T) (out T) {
	t.Helper()
	var buf bytes.Buffer
	if _, err := saveGraph(ctx, &buf, &in); err != nil {
		t.Fatalf("saveGraph: %v", err)
	}
	wr := wire.Reader{Reader: &buf}
	hdr, err := ReadHeaderMode(&wr)
	if err != nil || !hdr.Graph || !hdr.Object {
		t.Fatalf("header=%v err=%v", hdr, err)
	}
	if _, err := loadGraph(ctx, &buf, &out, hdr.Length, 0); err != nil {
		t.Fatalf("loadGraph: %v", err)
	}
	return out
}

func qaPartialWindows[T comparable](t *testing.T, marker T) {
	for _, test := range []struct {
		name string
		left int
	}{{"left_first", 0}, {"right_first", 1}} {
		t.Run(test.name, func(t *testing.T) {
			var buf [4]T
			windows := make([][]T, 2)
			windows[test.left], windows[1-test.left] = buf[:3:3], buf[1:4:4]
			got := qaRoundTrip(t, t.Context(), windows)
			left, right := got[test.left], got[1-test.left]
			for i := range 2 {
				if &left[i+1] != &right[i] {
					t.Errorf("lost shared address for element %d", i)
				}
				left[i+1] = marker
				if right[i] != marker {
					t.Errorf("lost write through left[%d]", i+1)
				}
			}
		})
	}
}

func TestQADecoderReview(t *testing.T) {
	RegisterStruct((*qaMapViews)(nil))
	RegisterStruct((*qaWord)(nil))
	RegisterStruct((*qaParent)(nil))
	fingerprint.Store(0)
	t.Run("map_views", func(t *testing.T) {
		plain := map[int]int{1: 7}
		got := qaRoundTrip(t, t.Context(), &qaMapViews{plain: plain, named: qaNamedMap(plain), other: qaNamedMap{1: 7}})
		got.named[2] = 9
		delete(got.plain, 1)
		if _, present := got.named[1]; present || got.plain[2] != 9 || !reflect.DeepEqual(got.other, qaNamedMap{1: 7}) {
			t.Errorf("plain=%v named=%v other=%v", got.plain, got.named, got.other)
		}
	})
	t.Run("three_bytes", func(t *testing.T) { qaPartialWindows(t, [3]byte{1}) })
	t.Run("four_bytes", func(t *testing.T) { qaPartialWindows(t, [4]byte{1}) })
	for _, dest := range []string{"interface_slot", "map_value", "map_pointer", "waiting_parent"} {
		t.Run(dest, func(t *testing.T) {
			o := &qaReport{token: 73}
			ctx := context.WithValue(t.Context(), qaContextKey{}, o)
			src := qaWord{p: &o.token}
			var got *int
			switch dest {
			case "interface_slot":
				got = qaRoundTrip[any](t, ctx, src).(qaWord).p
			case "map_value":
				got = qaRoundTrip(t, ctx, map[uint8]any{7: src})[7].(qaWord).p
			case "map_pointer":
				got = qaRoundTrip(t, ctx, map[uint8]any{7: &src})[7].(*qaWord).p
			case "waiting_parent":
				got = qaRoundTrip(t, ctx, &qaParent{values: map[uint8]any{7: src}}).values[7].(qaWord).p
				if o.parentCalls != 1 || o.wordsSeenByParent != 1 || !o.parentSawToken {
					t.Errorf("parent hooks=%d word hooks seen=%d parent saw restored pointer=%v", o.parentCalls, o.wordsSeenByParent, o.parentSawToken)
				}
			}
			t.Logf("word hooks=%d restored pointer=%v", o.wordCalls, got == &o.token)
			if o.wordCalls != 1 || got != &o.token {
				t.Errorf("want exactly one hook and the restored token")
			}
		})
	}
}

type leftSlotView []*byte
type rightSlotView []*byte

func (*leftSlotView) StateTypeName() string  { return "tests.leftSlotView" }
func (*leftSlotView) StateFields() []string  { return nil }
func (*rightSlotView) StateTypeName() string { return "tests.rightSlotView" }
func (*rightSlotView) StateFields() []string { return nil }

func init() {
	Register((*leftSlotView)(nil))
	Register((*rightSlotView)(nil))
}

// TestSliceArrayPointerWindows saves overlapping views without separately
// saving their backing array. The last pointee is reachable only on the right.
func TestSliceArrayPointerWindows(t *testing.T) {
	for _, kind := range []string{"slice", "array"} {
		for _, first := range []string{"left", "right"} {
			t.Run(kind+"/"+first, func(t *testing.T) {
				values := [4]byte{4, 8, 15, 16}
				buf := [4]*byte{&values[0], &values[1], &values[2], &values[3]}
				left := leftSlotView(buf[:3:3])
				right := rightSlotView(buf[1:4:4])
				var rv any = right
				if kind == "array" {
					rv = (*[3]*byte)(right)
				}
				li, ri := 0, 1
				if first == "right" {
					li, ri = 1, 0
				}
				var root [2]any
				root[li], root[ri] = left, rv
				got := qaRoundTrip(t, t.Context(), root)
				l := got[li].(leftSlotView)
				var r []*byte
				if kind == "array" {
					r = got[ri].(*[3]*byte)[:]
				} else {
					r = got[ri].(rightSlotView)
				}
				if len(l) != 3 || cap(l) != 3 || len(r) != 3 || cap(r) != 3 ||
					&l[1] != &r[0] || &l[2] != &r[1] || r[2] == nil || *r[2] != 16 {
					t.Fatalf("restored views lost their shared slots or final pointee: %v, %v", l, r)
				}
				l[1] = new(byte)
				if r[0] != l[1] {
					t.Fatal("updating a shared slot through the left view did not update the right")
				}
			})
		}
	}
}
