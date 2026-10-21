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

// Package containers provides imported containers with private child types.
package containers

type key string
type value uint32

// Mapping prevents the consumer from spelling its key and slice element types.
type Mapping map[key][]value

// Alias exercises aliases without introducing a second method owner.
type Alias = Mapping

// Pointer exercises a defined pointer to a private type.
type Pointer *value

// Array exercises an imported array with a private element type.
type Array [2]value

// NewMapping returns a populated mapping.
func NewMapping() Mapping { return Mapping{"first": {7, 11}} }

// NewPointer returns an addressable private value.
func NewPointer() Pointer { v := value(13); return &v }

// NewArray returns a populated array.
func NewArray() Array { return Array{17, 19} }

// Recursive exercises a private, mutually recursive imported slice element.
// +stateify type
type Recursive map[key]recursiveValues

// +stateify type
type recursiveValues []Recursive

// NewRecursive returns a graph containing a cycle through a private slice.
func NewRecursive() Recursive {
	root := make(Recursive)
	root["cycle"] = recursiveValues{root}
	return root
}

// Cycle returns the map reached through the private slice type.
func (r Recursive) Cycle() Recursive { return r["cycle"][0] }
