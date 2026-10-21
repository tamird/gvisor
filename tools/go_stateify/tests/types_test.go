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
	"reflect"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/tools/go_stateify/tests/containers"
)

func TestImportedDestinationTypes(t *testing.T) {
	// Calling through nil also proves that metadata generation does not read
	// the receiver, including named maps and their unnameable child types.
	var owner *object
	owner.StateDecodeTypes(new(state.DecoderTypes))

	mapping := containers.NewMapping()
	original := object{
		Mapping:   mapping,
		Alias:     mapping,
		Pointer:   containers.NewPointer(),
		Arrays:    []containers.Array{containers.NewArray()},
		Recursive: recursive{"child": recursive{"leaf": nil}},
		Custom:    containers.NewMapping(),
	}
	var encoded bytes.Buffer
	if _, err := state.Save(t.Context(), &encoded, &original); err != nil {
		t.Fatal(err)
	}
	var restored object
	if _, err := state.Load(t.Context(), &encoded, &restored); err != nil {
		t.Fatal(err)
	}
	if got, want := restored, original; !reflect.DeepEqual(got, want) {
		t.Fatalf("restored = %#v, want %#v", got, want)
	}
	delete(restored.Mapping, "first")
	if got, want := len(restored.Alias), 0; got != want {
		t.Errorf("aliased map length = %d, want %d", got, want)
	}
}
