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

import "testing"

type MyT struct{}

type intMethod struct{}

func (intMethod) M(int) {}

func TestForwardTypeParams(t *testing.T) {
	got := UseForward(intMethod{}, 42)
	if got.second != 42 {
		t.Errorf("UseForward returned %d, want 42", got.second)
	}
}

var externalCallbacks = []func() int{func() int { return 3 }}

func TestIndexedCalls(t *testing.T) {
	if got := UseIndexedCalls(); got != 10 {
		t.Errorf("UseIndexedCalls returned %d, want 10", got)
	}
}
