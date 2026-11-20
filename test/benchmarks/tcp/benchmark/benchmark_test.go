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
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestSenderRate(t *testing.T) {
	for _, tc := range []struct {
		name, result string
		want         float64
		wantError    bool
	}{
		{name: "aggregate", result: `{"end":{"sum_sent":{"bits_per_second":2418000000},"streams":[{"sender":{"bits_per_second":1258000000}},{"sender":{"bits_per_second":1160000000}}]}}`, want: 2418},
		{name: "zero", result: `{"end":{"sum_sent":{"bits_per_second":0}}}`},
		{name: "missing", result: `{"end":{"sum_received":{"bits_per_second":2418000000}}}`, wantError: true},
		{name: "error", result: `{"error":"server disconnected"}`, wantError: true},
		{name: "negative", result: `{"end":{"sum_sent":{"bits_per_second":-1}}}`, wantError: true},
		{name: "invalid", result: `not JSON`, wantError: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := senderRate([]byte(tc.result))
			if (err != nil) != tc.wantError || got != tc.want {
				t.Errorf("senderRate(%s) = (%v, %v), want (%v, error=%t)", tc.result, got, err, tc.want, tc.wantError)
			}
		})
	}
}

func TestParse(t *testing.T) {
	for _, duration := range []string{"030", "08"} {
		o, err := parse([]string{"--duration=" + duration})
		want, _ := strconv.Atoi(duration)
		if err != nil || o.duration != want {
			t.Fatalf("parse decimal duration %q = (%+v, %v), want %d seconds", duration, o, err, want)
		}
	}
	for _, args := range [][]string{{"--duration=0"}, {"--duration=86401"}, {"--num-client-threads=0"}, {"unexpected"}} {
		if _, err := parse(args); err == nil {
			t.Errorf("parse(%q) succeeded", args)
		}
	}
	dir := t.TempDir()
	o, err := parse([]string{"--helpers", dir, "--client", "--server", "--linux-client", "--ipv6", "--ideal", "--loss=5", "--sack", "--rack=false", "--moderate-recv-buf", "--sniff", "--cpuprofile=profile", "--num-client-threads=2", "--duration=86400"})
	if err != nil {
		t.Fatal(err)
	}
	if !o.client || !o.server || !o.linuxClient || !o.ipv6 || o.mtu != 1500 || o.latency != 0 || o.loss != 5 || o.streams != 2 || o.duration != 86400 {
		t.Errorf("parsed options = %+v", o)
	}
	if o.proxy != filepath.Join(dir, "tcp_proxy") || o.nsjoin != filepath.Join(dir, "nsjoin") {
		t.Errorf("helper paths = (%q, %q), want directory %q", o.proxy, o.nsjoin, dir)
	}
	args := strings.Join(o.proxyArgs("client"), " ")
	for _, want := range []string{"-sack=true", "-rack=false", "-moderate_recv_buf=true", "-sniff=true", "-cpuprofile=profile", "-forward [fd::3]:44000"} {
		if !strings.Contains(args, want) {
			t.Errorf("proxy arguments %q missing %q", args, want)
		}
	}
}
