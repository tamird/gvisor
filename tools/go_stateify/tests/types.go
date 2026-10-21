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

// Package tests checks generated destination operations across import boundaries.
package tests

import (
	"context"

	"gvisor.dev/gvisor/tools/go_stateify/tests/containers"
)

// +stateify type
type recursive map[string]recursive

// +stateify savable
type object struct {
	Mapping   containers.Mapping
	Alias     containers.Alias
	Pointer   containers.Pointer
	Arrays    []containers.Array
	Recursive recursive
	Imported  containers.Recursive
	Scalars   scalars
	Custom    any `state:".(containers.Mapping)"`
}

func (o *object) saveCustom() containers.Mapping                         { return o.Custom.(containers.Mapping) }
func (o *object) loadCustom(_ context.Context, value containers.Mapping) { o.Custom = value }

// +stateify savable
type scalars struct {
	Bool       bool
	Int8       int8
	Uint16     uint16
	Uintptr    uintptr
	Float32    float32
	Float64    float64
	Complex64  complex64
	Complex128 complex128
	Text       string
}

// +stateify savable
// +stateify identtype
type forwardedObject object
