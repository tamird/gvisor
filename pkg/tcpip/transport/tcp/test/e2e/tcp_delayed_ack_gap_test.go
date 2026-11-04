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

package tcp_delayed_ack_test

import (
	"runtime"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/checker"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp/test/e2e"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp/testing/context"
)

func TestDelayedACKGapQualification(t *testing.T) {
	for _, arm := range []struct {
		name  string
		optIn bool
	}{{"default", false}, {"enabled", true}} {
		t.Run(arm.name, func(t *testing.T) {
			clock := faketime.NewManualClock()
			c := context.NewWithOpts(t, context.Options{EnableV4: true, EnableV6: true, MTU: e2e.DefaultMTU, Clock: clock})
			defer c.Cleanup()
			if arm.optIn {
				on := tcpip.TCPDelayedACKOption(true)
				if err := c.Stack().SetTransportProtocolOption(tcp.ProtocolNumber, &on); err != nil {
					t.Fatal(err)
				}
			}
			var got tcpip.TCPDelayedACKOption
			if err := c.Stack().TransportProtocolOption(tcp.ProtocolNumber, &got); err != nil || bool(got) != arm.optIn {
				t.Fatalf("delayed ACK: %v, %v", got, err)
			}
			c.CreateConnected(context.TestInitialSequenceNumber, 30000, -1)
			// Use the author's request/response exchange to warm the connection.
			sendData(c, 0, 0)
			checkACK(t, c, payloadSize, 0, header.TCPFlagAck)
			respond(t, c, payloadSize, 0)
			n := seqnum.Value(context.TestInitialSequenceNumber).Add(1 + payloadSize)
			inject := func(seq seqnum.Value) {
				c.SendPacket([]byte{42}, &context.Headers{
					SrcPort: context.TestPort, DstPort: c.Port, Flags: header.TCPFlagAck,
					SeqNum: seq, AckNum: c.IRS.Add(1 + responseSize), RcvWnd: 30000,
				})
			}
			inject(n.Add(1))
			checkSegment(t, c, responseSize, payloadSize, 0, header.TCPFlagAck)
			inject(n)
			// This query acquires the endpoint lock. Both newly readable bytes
			// prove the receiver processed the gap before we advance the clock.
			deadline := time.Now().Add(5 * time.Second)
			for {
				size, err := c.EP.GetSockOptInt(tcpip.ReceiveQueueSizeOption)
				if err != nil || time.Now().After(deadline) {
					t.Fatalf("waiting for both bytes: got %d, %v", size, err)
				}
				if size == 2 {
					break
				}
				runtime.Gosched()
			}
			offsetMs := 0
			packet := c.GetPacketNonBlocking()
			if packet == nil {
				if !arm.optIn {
					t.Fatal("default did not immediately acknowledge the closed gap")
				}
				clock.Advance(delayedACKTimeout - time.Millisecond)
				c.CheckNoPacketTimeout("cumulative ACK before 40 ms", noPacketWait)
				clock.Advance(time.Millisecond)
				packet = c.GetPacket()
				offsetMs = 40
			}
			defer packet.Release()
			checker.IPv4(t, packet, checker.PayloadLen(header.TCPMinimumSize), checker.TCP(
				checker.TCPFlags(header.TCPFlagAck), checker.DstPort(context.TestPort),
				checker.TCPSeqNum(uint32(c.IRS)+1+responseSize), checker.TCPAckNum(uint32(n.Add(2))),
			))
			clock.Advance(delayedACKTimeout)
			c.CheckNoPacketTimeout("duplicate cumulative ACK", noPacketWait)
			t.Logf("GAP_ACK mode=%s gap_bytes=2 duplicate_ack=%d cumulative_ack=%d at_ms=%d", arm.name, n, n.Add(2), offsetMs)
		})
	}
}
