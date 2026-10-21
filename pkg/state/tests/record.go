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
	"context"

	"gvisor.dev/gvisor/pkg/state"
)

// The forwarding owner precedes its underlying type's registration. Capture
// must not depend on init statement order.
// +stateify savable
// +stateify identtype
type forwardedRecord recordParent

// +stateify savable
type recordParent struct {
	value int
	child *recordParent
}

// +stateify savable
type recordChild struct{ value int }

// +stateify type
type composedRecord struct {
	value   int
	before  int
	after   int
	replace bool
}

func (r *composedRecord) StateSave(s state.Sink) {
	// Forwarding must replace even a zero prefix without clearing siblings.
	s.Save(0, &r.before)
	s.Save(1, &r.before)
	child := recordChild{value: r.value}
	if r.replace {
		child.value++
	}
	child.StateSave(s)
	if r.replace {
		s.Save(0, &r.value)
	}
	s.Save(2, &r.after)
	s.Save(3, &r.replace)
}

func (r *composedRecord) StateLoad(_ context.Context, s state.Source) {
	s.Load(0, &r.value)
	s.Load(1, &r.before)
	s.Load(2, &r.after)
	s.Load(3, &r.replace)
}
