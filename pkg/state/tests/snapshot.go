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
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/wire"
)

// The conflict fixture intentionally supplies a manual saver. Other fixtures
// use the ordinary generated StateSave method.
type integerSnapshot struct{ value wire.Int }

func emitInteger(w *wire.Writer, s *integerSnapshot) {
	wire.SaveIntField(w, s.value)
}

// +stateify savable
type conflictingSnapshot struct {
	value int64
	save  func(state.Sink, *int64) `state:"nosave"`
}

func (v *conflictingSnapshot) StateSave(s state.Sink) {
	v.save(s, &v.value)
}
