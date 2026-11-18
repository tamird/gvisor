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

package codegen

import (
	"errors"
	"testing"

	"gvisor.dev/gvisor/tools/checkescape"
)

func TestValidateCalls(t *testing.T) {
	const caller = kvm + ".seccompMmapHandler"
	for _, test := range []struct {
		name string
		calls map[string][]checkescape.ObjectCall
		accepted bool
		wrapper bool
	}{
		{"no_calls", map[string][]checkescape.ObjectCall{caller: {}}, true, false},
		{"ordinary_call", map[string][]checkescape.ObjectCall{caller: {{Target: "runtime.atomicstorep", Resolved: true}}}, true, false},
		{"wrapper", map[string][]checkescape.ObjectCall{caller: {{Target: kvm + ".(*machineAtomicPtr).Load", Resolved: true}}}, false, true},
		{"generic_helper", map[string][]checkescape.ObjectCall{caller: {{Target: "sync/atomic.(*Pointer[go.shape.int]).Load", Resolved: true}}}, false, true},
		{"unresolved", map[string][]checkescape.ObjectCall{caller: {{Target: "", Resolved: false}}}, false, false},
		{"missing_owner", nil, false, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			err := ValidateCalls(test.calls, []string{caller})
			if (err == nil) != test.accepted {
				t.Fatalf("ValidateCalls() = %v, want accepted=%t", err, test.accepted)
			}
			var wrapper *WrapperCall
			if errors.As(err, &wrapper) != test.wrapper {
				t.Fatalf("ValidateCalls() = %v, want wrapper failure=%t", err, test.wrapper)
			}
		})
	}
}
