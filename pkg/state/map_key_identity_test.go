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
