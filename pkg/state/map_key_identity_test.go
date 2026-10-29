// Copyright 2026 The gVisor Authors.
// SPDX-License-Identifier: Apache-2.0

package state

import (
	"bytes"
	"context"
	"testing"
)

type qaKey struct{ point *uint64 }

type qaKeyReport struct {
	points [3]uint64
	hooks  [3]int
	late   bool
}
type qaKeyCtx struct{}

func (*qaKey) StateTypeName() string { return "state.qaKeyIdentity" }
func (*qaKey) StateFields() []string { return []string{"point"} }

func (k *qaKey) StateSave(s Sink) { s.SaveValue(0, *k.point) }

func (k *qaKey) StateLoad(ctx context.Context, s Source) {
	var id uint64
	s.Load(0, &id)
	r := ctx.Value(qaKeyCtx{}).(*qaKeyReport)
	restore := func() {
		r.hooks[id]++
		k.point = &r.points[id]
	}
	if r.late {
		s.AfterLoad(restore)
	} else {
		restore()
	}
}

func init() { RegisterStruct((*qaKey)(nil)) }

func TestMapKeyIdentity(t *testing.T) {
	const wantGraph = false
	for _, tc := range []struct {
		name string
		late bool
	}{{"immediate", false}, {"after_load", true}} {
		t.Run(tc.name, func(t *testing.T) {
			r := &qaKeyReport{points: [3]uint64{0, 1, 2}, late: tc.late}
			ctx := context.WithValue(t.Context(), qaKeyCtx{}, r)
			a, b := qaKey{&r.points[1]}, qaKey{&r.points[2]}
			want := map[qaKey]string{a: "left", b: "right"}
			var buf bytes.Buffer
			saved, err := Save(ctx, &buf, &want)
			if err != nil {
				t.Fatal(err)
			}
			if saved.graph.used != wantGraph {
				t.Fatalf("save graph=%t, want %t", saved.graph.used, wantGraph)
			}
			var got map[qaKey]string
			loaded, err := Load(ctx, &buf, &got)
			if err != nil {
				t.Fatal(err)
			}
			if loaded.graph.used != wantGraph {
				t.Fatalf("load graph=%t, want %t", loaded.graph.used, wantGraph)
			}
			t.Logf("control=%s hooks=%v entries=%d left=%q right=%q zero=%q", tc.name, r.hooks, len(got), got[a], got[b], got[qaKey{}])
			if r.hooks != [3]int{0, 1, 1} {
				t.Fatalf("restored keys %v, want one hook per key", r.hooks)
			}
			if len(got) != 2 || got[a] != "left" || got[b] != "right" {
				t.Errorf("distinct keys did not round-trip")
			}
		})
	}
}

type qaConverted struct {
	key       qaKey
	array     [1]qaKey
	boxed     any
	reference *qaKey
	scalar    int

	atConversion [4]*uint64
	atHook       [4]*uint64
	immediate    bool
}

func (*qaConverted) StateTypeName() string { return "state.qaConverted" }
func (*qaConverted) StateFields() []string {
	return []string{"key", "array", "boxed", "reference", "scalar"}
}

func (q *qaConverted) StateSave(s Sink) {
	s.Save(0, &q.key)
	s.Save(1, &q.array)
	s.Save(2, &q.boxed)
	s.Save(3, &q.reference)
	s.Save(4, &q.scalar)
}

func (q *qaConverted) StateLoad(_ context.Context, s Source) {
	// Register first: fields must still finish before the enclosing hook.
	s.AfterLoad(func() { q.atHook = q.atConversion })
	s.LoadValue(0, new(qaKey), func(v any) { q.atConversion[0] = v.(qaKey).point })
	s.LoadValue(1, new([1]qaKey), func(v any) { q.atConversion[1] = v.([1]qaKey)[0].point })
	s.LoadValue(2, new(any), func(v any) { q.atConversion[2] = v.(qaKey).point })
	s.LoadValue(3, new(*qaKey), func(v any) { q.atConversion[3] = v.(*qaKey).point })
	s.LoadValue(4, new(int), func(v any) { q.scalar = v.(int) })
	q.immediate = q.scalar == 7
}

func init() { RegisterStruct((*qaConverted)(nil)) }

func TestLoadValueFieldCompletion(t *testing.T) {
	r := &qaKeyReport{points: [3]uint64{0, 1, 2}, late: true}
	ctx := context.WithValue(t.Context(), qaKeyCtx{}, r)
	k := qaKey{&r.points[1]}
	want := qaConverted{key: k, array: [1]qaKey{k}, boxed: k, reference: &qaKey{&r.points[1]}, scalar: 7}
	var buf bytes.Buffer
	if _, err := Save(ctx, &buf, &want); err != nil {
		t.Fatal(err)
	}
	var got qaConverted
	if _, err := Load(ctx, &buf, &got); err != nil {
		t.Fatal(err)
	}
	if !got.immediate {
		t.Error("scalar field conversion was postponed")
	}
	if got, want := r.hooks[1], 4; got != want {
		t.Errorf("completed hooks = %d, want %d", got, want)
	}
	for i, field := range []string{"struct", "array", "interface", "reference"} {
		point := &r.points[1]
		t.Run(field, func(t *testing.T) {
			if x := got.atConversion[i]; x != point {
				t.Errorf("LoadValue copied %p, want %p", x, point)
			}
			if x := got.atHook[i]; x != point {
				t.Errorf("enclosing hook saw %p, want %p", x, point)
			}
		})
	}
}
