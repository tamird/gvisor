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
	mapping := containers.NewMapping()
	original := forwardedObject{
		Mapping:   mapping,
		Alias:     mapping,
		Pointer:   containers.NewPointer(),
		Arrays:    []containers.Array{containers.NewArray()},
		Recursive: recursive{"child": recursive{"leaf": nil}},
		Imported:  containers.NewRecursive(),
		Scalars:   scalars{true, -3, 17, 23, 1.25, -7.5, complex(2, 3), complex(-4, 5), "typed"},
		Custom:    containers.NewMapping(),
	}
	var encoded bytes.Buffer
	if _, err := state.Save(t.Context(), &encoded, &original); err != nil {
		t.Fatal(err)
	}
	var restored forwardedObject
	if _, err := state.Load(t.Context(), &encoded, &restored); err != nil {
		t.Fatal(err)
	}
	if got, want := restored, original; !reflect.DeepEqual(got, want) {
		t.Fatalf("restored = %#v, want %#v", got, want)
	}
	first, second := restored.Imported.Child("first"), restored.Imported.Child("second")
	delete(first, "leaf")
	if got, want := len(second), 0; got != want {
		t.Errorf("recursive alias length = %d, want %d", got, want)
	}
	delete(restored.Mapping, "first")
	if got, want := len(restored.Alias), 0; got != want {
		t.Errorf("aliased map length = %d, want %d", got, want)
	}
}
