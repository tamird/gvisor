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

func TestSnapshotAliases(t *testing.T) {
	makeGraph := func() *system3 {
		parent := &outerFieldFirst{inner: inner{v: -7}, v: 11}
		custom := &innerFieldValue{v: 19}
		return &system3{
			// Discover the interior object before its containing struct.
			v1: &parent.inner,
			v2: parent,
			v3: &system{v1: custom, v2: custom},
		}
	}
	var snapshots bytes.Buffer
	graph := makeGraph()
	if _, err := state.Save(t.Context(), &snapshots, &graph); err != nil {
		t.Fatal(err)
	}
	var loaded *system3
	if _, err := state.Load(t.Context(), &snapshots, &loaded); err != nil {
		t.Fatal(err)
	}
	parent := loaded.v2.(*outerFieldFirst)
	if got, want := loaded.v1.(*inner), &parent.inner; got != want {
		t.Fatalf("interior alias = %p, want %p", got, want)
	}
	custom := loaded.v3.(*system)
	if got, want := custom.v1.(*innerFieldValue), custom.v2.(*innerFieldValue); got != want {
		t.Fatalf("custom alias = %p, want %p", got, want)
	}
	if got, want := custom.v1.(*innerFieldValue).v, int64(19); got != want {
		t.Fatalf("custom value = %d, want %d", got, want)
	}
}

func TestGeneratedRecordForwarding(t *testing.T) {
	child := &recordParent{value: 31}
	runTestCases(t, false, "identtype", []any{
		forwardedRecord{value: 17, child: child},
		composedRecord{value: 17, before: 11, after: 23},
		composedRecord{value: 17, before: 11, after: 23, replace: true},
		composedRecord{value: 0, before: 11, after: 23},
	})
}
