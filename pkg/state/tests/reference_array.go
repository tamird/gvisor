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

type referenceIntPointer *int

// +stateify type
type referenceArrayEncoding struct {
	ordinary [4]*int
	named    [4]referenceIntPointer
	byValue  bool `state:"nosave"`
}

func (a *referenceArrayEncoding) StateSave(s state.Sink) {
	if a.byValue {
		s.SaveValue(0, a.ordinary)
		s.SaveValue(1, a.named)
	} else {
		s.Save(0, &a.ordinary)
		s.Save(1, &a.named)
	}
}

func (a *referenceArrayEncoding) StateLoad(_ context.Context, s state.Source) {
	s.Load(0, &a.ordinary)
	s.Load(1, &a.named)
}

// +stateify savable
type referenceArraySource struct {
	values [2]*int
}

// +stateify savable
type referenceArrayMutator struct {
	target      *referenceArraySource
	replacement *int
}

func (m *referenceArrayMutator) beforeSave() {
	m.target.values[0] = m.replacement
}

// +stateify savable
type referenceArrayDiscovery struct {
	values [][2]*int
}
