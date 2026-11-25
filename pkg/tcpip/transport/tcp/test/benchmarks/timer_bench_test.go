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

package tcp_benchmarks_test

import (
	"io"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
)

// BenchmarkConnectionSetup times bind/listen/connect/accept on existing stacks.
// Stack creation, packet-pump setup and teardown are outside the timed region.
func BenchmarkConnectionSetup(b *testing.B) {
	b.ReportAllocs()
	b.StopTimer()
	for i := 0; i < b.N; i++ {
		n := newTopology(b, false, 0, nil)
		b.StartTimer()
		_, err := n.connect(n.ctx)
		b.StopTimer()
		n.close()
		if err != nil {
			b.Fatal(err)
		}
	}
}

// BenchmarkDelayedACKCallback times real callback processing, not the simulated
// 40ms delay. Packet setup and teardown are outside the timed region.
func BenchmarkDelayedACKCallback(b *testing.B) {
	b.ReportAllocs()
	b.StopTimer()
	for i := 0; i < b.N; i++ {
		clock := faketime.NewManualClock()
		n := newTopology(b, false, 0, clock)
		enabled := tcpip.TCPDelayedACKEnabled(true)
		if err := n.stacks[1].SetTransportProtocolOption(tcp.ProtocolNumber, &enabled); err != nil {
			b.Fatal(err)
		}
		pair, err := n.connect(n.ctx)
		if err != nil {
			b.Fatal(err)
		}
		pair.server.ep.SocketOptions().SetQuickAck(false)
		if err := pair.client.write(n.ctx, []byte{1}); err != nil {
			b.Fatal(err)
		}
		if err := pair.server.read(n.ctx, io.Discard, 1); err != nil {
			b.Fatal(err)
		}
		stats := n.stacks[1].Stats().TCP
		if got := stats.DelayedACKs.Value(); got != 0 {
			b.Fatalf("DelayedACKs before expiry = %d, want 0", got)
		}
		b.StartTimer()
		clock.Advance(40 * time.Millisecond)
		b.StopTimer()
		got := stats.DelayedACKs.Value()
		n.close()
		if got != 1 {
			b.Fatalf("DelayedACKs after expiry = %d, want 1", got)
		}
	}
}
