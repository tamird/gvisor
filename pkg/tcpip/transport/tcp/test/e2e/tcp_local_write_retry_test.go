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

package tcp_test

import (
	"bytes"
	"sync/atomic"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	testcontext "gvisor.dev/gvisor/pkg/tcpip/transport/tcp/testing/context"
)

type gsoWriteTest struct {
	*testcontext.Context
	t                *testing.T
	clock            *faketime.ManualClock
	refuse, allowOne atomic.Bool
	probing          atomic.Bool
	sender           chan tcp.TCPSenderState
	mss              int
	data             []byte
}

func newGSOWriteTest(t *testing.T) *gsoWriteTest {
	t.Helper()
	g := &gsoWriteTest{t: t, clock: faketime.NewManualClock(), sender: make(chan tcp.TCPSenderState, 1)}
	g.Context = testcontext.NewWithOpts(t, testcontext.Options{
		EnableV4: true, MTU: 1500, GSO: stack.GVisorGSOSupported, Clock: g.clock,
		LinkWriteError: func(ps stack.PacketBufferList) tcpip.Error {
			for _, p := range ps.AsSlice() {
				if p.Data().Size() != 0 && g.refuse.Load() && !g.allowOne.Swap(false) {
					return &tcpip.ErrNoBufferSpace{}
				}
			}
			return nil
		},
		Probe: func(e *tcp.TCPEndpointState) {
			if g.probing.Swap(false) {
				g.sender <- e.Sender
			}
		},
	})
	t.Cleanup(g.Cleanup)
	// A timer retry in these tests must be a retransmission timeout, not a tail-loss probe.
	loss := tcpip.TCPRecovery(0)
	if err := g.Stack().SetTransportProtocolOption(tcp.ProtocolNumber, &loss); err != nil {
		t.Fatal(err)
	}
	g.CreateConnected(testcontext.TestInitialSequenceNumber, 30000, -1)
	var info tcpip.TCPInfoOption
	if err := g.EP.GetSockOpt(&info); err != nil {
		t.Fatal(err)
	}
	g.mss = int(info.SndMss)
	g.data = make([]byte, 3*g.mss)
	for i := range g.data {
		g.data[i] = byte(i/g.mss*63 + i%101)
	}
	return g
}

func (g *gsoWriteTest) write(data []byte) {
	g.t.Helper()
	if n, err := g.EP.Write(bytes.NewReader(data), tcpip.WriteOptions{}); err != nil || int(n) != len(data) {
		g.t.Fatalf("socket write = %d, %v; want %d, nil", n, err, len(data))
	}
}

func (g *gsoWriteTest) advanceRTO() {
	g.t.Helper()
	var info tcpip.TCPInfoOption
	if err := g.EP.GetSockOpt(&info); err != nil {
		g.t.Fatal(err)
	}
	g.clock.Advance(info.RTO)
}


// TestLocalRetryMeetsPeerWindow records the probe at the first timer expiry.
// Both peers close their advertised window and then reopen it. Only one local
// link first refuses a write; allowing that link to send does not open the peer.
func TestLocalRetryMeetsPeerWindow(t *testing.T) {
	for _, reject := range []bool{false, true} {
		name := "peer-closes"
		if reject {
			name = "local-refusal-then-peer-closes"
		}
		t.Run(name, func(t *testing.T) {
			g := newGSOWriteTest(t)
			window := func(size seqnum.Size) {
				t.Helper()
				g.probing.Store(true)
				g.SendPacket(nil, &testcontext.Headers{
					SrcPort: testcontext.TestPort, DstPort: g.Port, Flags: header.TCPFlagAck,
					SeqNum: testcontext.TestInitialSequenceNumber + 1, AckNum: g.IRS.Add(1), RcvWnd: size,
				})
				select {
				case s := <-g.sender:
					if got, want := s.SndWnd, size; got != want {
						t.Fatalf("advertised window = %d, want %d", got, want)
					}
				case <-time.After(3 * time.Second):
					t.Fatal("window update did not reach TCP")
				}
				g.Stack().Pause()
				g.Stack().Resume()
			}
			data := g.data[:g.mss]
			if reject {
				g.refuse.Store(true)
				g.write(data)
				g.Stack().Pause()
				g.Stack().Resume()
				if got, want := g.Stack().Stats().TCP.SegmentSendErrors.Value(), uint64(1); got != want {
					t.Fatalf("local refusals = %d, want %d", got, want)
				}
				g.refuse.Store(false)
			}
			window(0)
			if !reject {
				g.write(data)
				g.Stack().Pause()
				g.Stack().Resume()
			}
			if p := g.GetPacketNonBlocking(); p != nil {
				p.Release()
				t.Fatal("packet before the probe deadline")
			}
			g.advanceRTO()
			g.Stack().Pause()
			g.Stack().Resume()
			p := g.GetPacketNonBlocking()
			sent := p != nil
			if sent {
				v := header.IPv4(p.AsSlice())
				th := header.TCP(v.Payload())
				if got, want := th.SequenceNumber(), uint32(g.IRS); got != want {
					t.Errorf("probe sequence = %d, want %d", got, want)
				}
				if got, want := len(th.Payload()), 1; got != want {
					t.Errorf("probe payload length = %d, want %d", got, want)
				}
				p.Release()
			}
			window(30000)
			g.ReceiveAndCheckPacket(data, 0, len(data))
			t.Logf("TCP-PERSIST refused=%t probe=%t resumed=%d", reject, sent, len(data))
		})
	}
}
