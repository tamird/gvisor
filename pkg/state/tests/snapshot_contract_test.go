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
	"strings"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
)

func TestConflictingSnapshot(t *testing.T) {
	for _, test := range []struct {
		name string
		save func(state.Sink, *int64)
		want string
	}{
		{
			name: "ordinary_then_snapshot",
			save: func(s state.Sink, value *int64) {
				s.Save(0, value)
				state.BeginSnapshot(s, emitInteger)
			},
			want: "typed snapshot cannot replace existing fields",
		},
		{
			name: "snapshot_twice",
			save: func(s state.Sink, _ *int64) {
				state.BeginSnapshot(s, emitInteger)
				state.BeginSnapshot(s, emitInteger)
			},
			want: "typed snapshot cannot replace existing fields",
		},
		{
			name: "snapshot_then_ordinary",
			save: func(s state.Sink, value *int64) {
				state.BeginSnapshot(s, emitInteger)
				s.Save(0, value)
			},
			want: "Field is unavailable on an immutable typed snapshot",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			original := conflictingSnapshot{value: 7, save: test.save}
			var encoded bytes.Buffer
			_, err := state.Save(t.Context(), &encoded, &original)
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("Save error = %v, want %q", err, test.want)
			}
		})
	}
}
