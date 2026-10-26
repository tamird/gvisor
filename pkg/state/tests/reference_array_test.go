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

package tests

import (
	"bytes"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
)

func TestReferenceArrayWire(t *testing.T) {
	first, second, third := 7, 11, 19
	original := referenceArrayEncoding{
		ordinary: [4]*int{nil, &first, &second, &first},
		named:    [4]referenceIntPointer{&third, nil, &first, &third},
	}
	var captured, reflected bytes.Buffer
	if _, err := state.Save(t.Context(), &captured, &original); err != nil {
		t.Fatal(err)
	}
	original.byValue = true
	if _, err := state.Save(t.Context(), &reflected, &original); err != nil {
		t.Fatal(err)
	}
	if got, want := captured.Bytes(), reflected.Bytes(); !bytes.Equal(got, want) {
		t.Fatalf("addressable encoding = %x, want value encoding %x", got, want)
	}
	var loaded referenceArrayEncoding
	if _, err := state.Load(t.Context(), &captured, &loaded); err != nil {
		t.Fatal(err)
	}
	if got, want := loaded.ordinary[0], (*int)(nil); got != want {
		t.Errorf("first reference = %p, want %p", got, want)
	}
	if got, want := loaded.named[1], referenceIntPointer(nil); got != want {
		t.Errorf("named nil reference = %p, want %p", got, want)
	}
	if loaded.ordinary[1] == nil || loaded.ordinary[2] == nil {
		t.Fatalf("loaded values = %v, want two non-nil references", loaded.ordinary)
	}
	if got, want := *loaded.ordinary[1], first; got != want {
		t.Errorf("first value = %d, want %d", got, want)
	}
	if got, want := *loaded.ordinary[2], second; got != want {
		t.Errorf("second value = %d, want %d", got, want)
	}
	if got, want := loaded.ordinary[3], loaded.ordinary[1]; got != want {
		t.Errorf("repeated reference = %p, want %p", got, want)
	}
	if got, want := loaded.named[2], referenceIntPointer(loaded.ordinary[1]); got != want {
		t.Errorf("named first alias = %p, want %p", got, want)
	}
	if loaded.named[0] == nil {
		t.Fatal("named-only reference is nil")
	}
	if got, want := *loaded.named[0], third; got != want {
		t.Errorf("named-only value = %d, want %d", got, want)
	}
	if got, want := loaded.named[3], loaded.named[0]; got != want {
		t.Errorf("repeated named alias = %p, want %p", got, want)
	}
}

func TestReferenceArraySnapshotTiming(t *testing.T) {
	first, second, replacement := 7, 11, 99
	source := &referenceArraySource{values: [2]*int{&first, &second}}
	original := system{v1: source, v2: &referenceArrayMutator{target: source, replacement: &replacement}}
	var encoded bytes.Buffer
	if _, err := state.Save(t.Context(), &encoded, &original); err != nil {
		t.Fatal(err)
	}
	if got, want := source.values[0], &replacement; got != want {
		t.Fatalf("later replacement = %p, want %p", got, want)
	}
	var loaded system
	if _, err := state.Load(t.Context(), &encoded, &loaded); err != nil {
		t.Fatal(err)
	}
	child := loaded.v1.(*referenceArraySource)
	if child.values[0] == nil || child.values[1] == nil {
		t.Fatalf("captured values = %v, want two non-nil references", child.values)
	}
	if got, want := *child.values[0], first; got != want {
		t.Errorf("first captured value = %d, want %d", got, want)
	}
	if got, want := *child.values[1], second; got != want {
		t.Errorf("second captured value = %d, want %d", got, want)
	}
	if got, want := loaded.v2.(*referenceArrayMutator).target, child; got != want {
		t.Errorf("shared target = %p, want %p", got, want)
	}
}

func TestReferenceArrayLateParent(t *testing.T) {
	first, second := 7, 11
	backing := [2][2]*int{{&first, &second}, {&second, &first}}
	// The later slice clears its unused capacity after the interior array
	// was encoded. Recapturing the parent must replace those old references.
	original := system{v1: &backing[1], v2: &referenceArrayDiscovery{values: backing[:1]}}
	var encoded bytes.Buffer
	if _, err := state.Save(t.Context(), &encoded, &original); err != nil {
		t.Fatal(err)
	}
	if got, want := backing[1], ([2]*int{}); got != want {
		t.Fatalf("cleared backing tail = %v, want %v", got, want)
	}
	var loaded system
	if _, err := state.Load(t.Context(), &encoded, &loaded); err != nil {
		t.Fatal(err)
	}
	child := loaded.v1.(*[2]*int)
	parent := loaded.v2.(*referenceArrayDiscovery).values
	if got, want := cap(parent), 2; got != want {
		t.Fatalf("backing capacity = %d, want %d", got, want)
	}
	if got, want := child, &parent[:2][1]; got != want {
		t.Errorf("interior array = %p, want %p", got, want)
	}
	if got, want := *child, ([2]*int{}); got != want {
		t.Errorf("recaptured tail = %v, want %v", got, want)
	}
}
