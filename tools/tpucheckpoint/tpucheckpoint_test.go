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

package tpucheckpoint_test

import (
	"errors"
	"os/exec"
	"testing"

	"gvisor.dev/gvisor/pkg/test/testutil"
)

func TestTimeout(t *testing.T) {
	bin, err := testutil.FindFile("tools/tpucheckpoint/tpucheckpoint")
	if err != nil {
		t.Fatal(err)
	}
	// Omit --pid so valid timeouts stop before process discovery or pipe I/O.
	for _, tc := range []struct {
		name string
		args []string
		want string
	}{
		{"default", nil, "--pid is required"},
		{"minimum", []string{"--timeout", "1"}, "--pid is required"},
		{"maximum", []string{"--timeout", "2147483647"}, "--pid is required"},
		{"positive_sign", []string{"-t", "+2147483647"}, `timeout must be in [1, 2147483647]: strconv.ParseUint: parsing "+2147483647": invalid syntax`},
		{"overflow", []string{"--timeout", "2147483648"}, `timeout must be in [1, 2147483647]: strconv.ParseUint: parsing "2147483648": value out of range`},
		{"wrap_to_zero", []string{"-t", "4294967296"}, `timeout must be in [1, 2147483647]: strconv.ParseUint: parsing "4294967296": value out of range`},
		{"duration_overflow", []string{"-t", "9223372036854775807"}, `timeout must be in [1, 2147483647]: strconv.ParseUint: parsing "9223372036854775807": value out of range`},
		{"invalid_syntax", []string{"-t", "abc"}, `timeout must be in [1, 2147483647]: strconv.ParseUint: parsing "abc": invalid syntax`},
		{"zero", []string{"-t", "0"}, `timeout must be in [1, 2147483647], got 0`},
		{"negative", []string{"-t", "-1"}, `timeout must be in [1, 2147483647]: strconv.ParseUint: parsing "-1": invalid syntax`},
		{"missing", []string{"--timeout"}, "--timeout requires a value"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			args := append([]string{"--get-state"}, tc.args...)
			output, err := exec.CommandContext(t.Context(), bin, args...).CombinedOutput()
			exit, ok := errors.AsType[*exec.ExitError](err)
			if !ok || exit.ExitCode() != 1 {
				t.Fatalf("tpucheckpoint %v: error = %v, want exit 1; output: %s", args, err, output)
			}
			if want := "tpucheckpoint: " + tc.want + "\n"; string(output) != want {
				t.Errorf("tpucheckpoint %v: output = %q, want %q", args, output, want)
			}
		})
	}
}
