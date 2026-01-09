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

package test

import (
	"bytes"
	"context"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
)

func TestGenericStateifyRoundTrip(t *testing.T) {
	value := 123
	saved := root{
		b: box[int]{Value: &value},
		p: plain{N: 42},
	}

	var buf bytes.Buffer
	if _, err := state.Save(context.Background(), &buf, &saved); err != nil {
		t.Fatalf("Save failed: %v", err)
	}

	var restored root
	if _, err := state.Load(context.Background(), &buf, &restored); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	if restored.b.Value == nil || *restored.b.Value != value {
		t.Fatalf("restored box value = %d, want %d", restored.b.Value, value)
	}
	if restored.p.N != saved.p.N {
		t.Fatalf("restored plain N = %d, want %d", restored.p.N, saved.p.N)
	}
}

func TestGenericTypeInterfaces(t *testing.T) {
	var _ state.Type = (*box[int])(nil)
	var _ state.SaverLoader = (*box[int])(nil)
	var _ state.Type = (*boxedInt)(nil)
	var _ state.SaverLoader = (*boxedInt)(nil)
}

func TestGenericInstantiationRoundTrip(t *testing.T) {
	value := 321
	saved := genericRoot{
		b: boxedInt(box[int]{Value: &value}),
	}

	var buf bytes.Buffer
	if _, err := state.Save(context.Background(), &buf, &saved); err != nil {
		t.Fatalf("Save failed: %v", err)
	}

	var restored genericRoot
	if _, err := state.Load(context.Background(), &buf, &restored); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	if restored.b.Value == nil || *restored.b.Value != value {
		t.Fatalf("restored boxed value = %d, want %d", restored.b.Value, value)
	}
}
