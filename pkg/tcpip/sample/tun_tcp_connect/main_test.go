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
	"syscall"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/testutil"
)

func TestPortValidationBeforeDeviceSetup(t *testing.T) {
	binary, err := testutil.FindFile("pkg/tcpip/sample/tun_tcp_connect/tun_tcp_connect")
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name      string
		port      string
		wantError string
	}{
		{name: "negative", port: "-1", wantError: "value out of range"},
		{name: "wrapped_zero", port: "65536", wantError: "value out of range"},
		{name: "integer_overflow", port: "18446744073709551616", wantError: "value out of range"},
		{name: "malformed", port: "not-a-port", wantError: "invalid syntax"},
		{name: "zero", port: "0", wantError: syscall.ENODEV.Error()},
		{name: "signed_zero", port: "-000", wantError: syscall.ENODEV.Error()},
		{name: "maximum", port: "65535", wantError: syscall.ENODEV.Error()},
		{name: "plus_and_zeros", port: "+0001", wantError: syscall.ENODEV.Error()},
	} {
		for _, portArg := range []struct {
			name  string
			index int
		}{{name: "local", index: 2}, {name: "remote", index: 4}} {
			t.Run(tc.name+"/"+portArg.name, func(t *testing.T) {
				// An empty interface name cannot exist. Valid ports reach
				// device setup; invalid ports must be rejected before it.
				args := []string{"", "127.0.0.1", "0", "127.0.0.1", "0"}
				args[portArg.index] = tc.port
				ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				output, err := exec.CommandContext(ctx, binary, args...).CombinedOutput()
				if ctx.Err() != nil {
					t.Fatalf("sample did not reject input: %v; output: %s", ctx.Err(), output)
				}
				if err == nil || !strings.Contains(string(output), tc.wantError) {
					t.Fatalf("sample output = %q, error = %v; want failure containing %q", output, err, tc.wantError)
				}
			})
		}
	}
}
