// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package pretty

import (
	"bytes"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/wire"
)

func TestInternalFieldTags(t *testing.T) {
	var input bytes.Buffer
	w := wire.Writer{Writer: &input}
	if err := state.WriteHeader(&w, 2, true); err != nil {
		t.Fatal(err)
	}
	// The wire printer also accepts field-list tags outside a Struct, where
	// their private Object implementations are unavailable to callers.
	wire.Save(&w, wire.Uint(1))
	wire.SaveUint(&w, uint64(wire.KindNoObjects))
	wire.Save(&w, wire.Uint(2))
	wire.SaveUint(&w, uint64(wire.KindMultipleObjects))
	wire.SaveUint(&w, 2)
	wire.Save(&w, wire.Int(17))
	text := wire.String("owned")
	wire.Save(&w, &text)
	var output bytes.Buffer
	if err := PrintText(&output, &input); err != nil {
		t.Fatal(err)
	}
	if got, want := output.String(), "g0r1 = {}\ng0r2 = [\n\t17,\n\t\"owned\",\n]\n"; got != want {
		t.Errorf("PrintText = %q, want %q", got, want)
	}
}
