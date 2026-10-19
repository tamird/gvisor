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

package main

import (
	"context"
	"os/exec"
	"strings"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/testutil"
)

func TestPortsBeforeNetworkSetup(t *testing.T) {
	binary, err := testutil.FindFile("test/benchmarks/tcp/tcp_proxy")
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name      string
		args      []string
		wantError string
	}{
		{name: "negative_native_listen", args: []string{"-client", "-port=-1"}, wantError: "value out of range"},
		{name: "wrapped_native_listen", args: []string{"-client", "-port=65536"}, wantError: "value out of range"},
		{name: "negative_netstack_listen", args: []string{"-server", "-port=-1"}, wantError: "value out of range"},
		{name: "wrapped_netstack_listen", args: []string{"-server", "-port=65536"}, wantError: "value out of range"},
		{name: "missing_listen", args: []string{"-port=0"}, wantError: "no port provided"},
		{name: "maximum_listen", args: []string{"-server", "-port=65535"}, wantError: "failed to find interface"},
		{name: "hex_listen", args: []string{"-server", "-port=0xffff"}, wantError: "failed to find interface"},
		{name: "padded_listen", args: []string{"-server", "-port=+0001"}, wantError: "failed to find interface"},
		{name: "negative_forward", args: []string{"-client", "-forward=127.0.0.1:-1"}, wantError: "value out of range"},
		{name: "wrapped_forward", args: []string{"-client", "-forward=127.0.0.1:65536"}, wantError: "value out of range"},
		{name: "malformed_forward", args: []string{"-client", "-forward=127.0.0.1:not-a-port"}, wantError: "invalid syntax"},
		{name: "zero_forward", args: []string{"-client", "-forward=127.0.0.1:0"}, wantError: "failed to find interface"},
		{name: "signed_zero_forward", args: []string{"-client", "-forward=127.0.0.1:-000"}, wantError: "failed to find interface"},
		{name: "maximum_forward", args: []string{"-client", "-forward=127.0.0.1:65535"}, wantError: "failed to find interface"},
		{name: "padded_forward", args: []string{"-client", "-forward=127.0.0.1:+0001"}, wantError: "failed to find interface"},
		{name: "ipv6_forward", args: []string{"-client", "-forward=[::1]:65535"}, wantError: "failed to find interface"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// No interface has an empty name. Valid ports reach network
			// setup; invalid ports must fail before any socket is opened.
			args := []string{"-iface=", "-addr=127.0.0.1", "-port=44000", "-forward=127.0.0.1:45201"}
			args = append(args, tc.args...)
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			defer cancel()
			output, err := exec.CommandContext(ctx, binary, args...).CombinedOutput()
			if ctx.Err() != nil {
				t.Fatalf("proxy did not reject input: %v; output: %s", ctx.Err(), output)
			}
			if err == nil || !strings.Contains(string(output), tc.wantError) {
				t.Fatalf("proxy output = %q, error = %v; want failure containing %q", output, err, tc.wantError)
			}
		})
	}
}
