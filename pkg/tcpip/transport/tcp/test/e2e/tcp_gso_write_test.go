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

// ack checks the sender state after the peer ACK reaches the endpoint.
func (g *gsoWriteTest) ack(ack, next, outstanding int) {
	g.t.Helper()
	g.probing.Store(true)
	g.SendAck(testcontext.TestInitialSequenceNumber+1, ack)
	select {
	case s := <-g.sender:
		if got, want := int(s.SndUna-(g.IRS+1)), ack; got != want {
			g.t.Errorf("SND.UNA = +%d, want +%d", got, want)
		}
		if got, want := int(s.SndNxt-(g.IRS+1)), next; got != want {
			g.t.Errorf("SND.NXT = +%d, want +%d", got, want)
		}
		if got, want := s.Outstanding, outstanding; got != want {
			g.t.Errorf("outstanding = %d, want %d", got, want)
		}
	case <-time.After(3 * time.Second):
		g.t.Fatal("no sender observation after ACK")
	}
	g.Stack().Pause()
	g.Stack().Resume()
}

// checkRefusal verifies that only the first MSS is in flight, even after a
// nonadvancing ACK gives the sender another chance to write the suffix.
func (g *gsoWriteTest) checkRefusal() {
	g.t.Helper()
	g.Stack().Pause()
	g.Stack().Resume()
	errors := g.Stack().Stats().TCP.SegmentSendErrors
	if got, want := errors.Value(), uint64(1); got != want {
		g.t.Fatalf("route errors after initial write = %d, want %d", got, want)
	}
	if p := g.GetPacketNonBlocking(); p != nil {
		p.Release()
		g.t.Fatal("received rejected data")
	}
	g.ack(0, g.mss, 1)
	if got, want := errors.Value(), uint64(2); got != want {
		g.t.Fatalf("route errors after duplicate ACK = %d, want %d", got, want)
	}
}

func (g *gsoWriteTest) drainSuffix(wantTimeouts uint64) {
	g.t.Helper()
	g.SendAck(testcontext.TestInitialSequenceNumber+1, g.mss)
	g.Stack().Pause()
	g.Stack().Resume()
	for off := g.mss; off < len(g.data); off += g.mss {
		g.ReceiveAndCheckPacket(g.data, off, g.mss)
	}
	g.ack(len(g.data), len(g.data), 0)
	if got := g.Stack().Stats().TCP.Timeouts.Value(); got != wantTimeouts {
		g.t.Fatalf("timeouts after final ACK = %d, want %d", got, wantTimeouts)
	}
}

func partialGSOWrite(t *testing.T) *gsoWriteTest {
	t.Helper()
	g := newGSOWriteTest(t)
	g.allowOne.Store(true)
	g.refuse.Store(true)
	g.write(g.data)
	g.ReceiveAndCheckPacket(g.data, 0, g.mss)
	g.checkRefusal()
	return g
}

func TestPartialGSOProgress(t *testing.T) {
	t.Run("ack", func(t *testing.T) {
		g := partialGSOWrite(t)
		g.refuse.Store(false)
		g.drainSuffix(0)
	})
	t.Run("timeout", func(t *testing.T) {
		g := partialGSOWrite(t)
		g.refuse.Store(false)
		g.advanceRTO()
		g.ReceiveAndCheckPacket(g.data, 0, g.mss)
		if got, want := g.Stack().Stats().TCP.Timeouts.Value(), uint64(1); got != want {
			t.Fatalf("timeouts = %d, want %d", got, want)
		}
		g.drainSuffix(1)
	})
	t.Run("restore", func(t *testing.T) {
		g := partialGSOWrite(t)
		g.Stack().SetAllowLiveTCPMigration(true)
		g.Stack().SetRemoveConf(true)
		restored, link := restoreGSORoute(t, g.Context, 1500, stack.GVisorGSOSupported)
		defer restored.Destroy()
		before := restored.Stats().TCP.Timeouts.Value()
		sendRestoredGSOAck(g.Context, restored, link, g.mss)
		for off := g.mss; off < len(g.data); off += g.mss {
			p := link.Read()
			if p == nil {
				t.Fatalf("no data at +%d after the restored prefix ACK", off)
			}
			if got, want := header.TCP(p.TransportHeader().Slice()).SequenceNumber(), uint32(g.IRS+1)+uint32(off); got != want {
				t.Errorf("restored sequence number = %d, want %d", got, want)
			}
			payload := p.Data().AsRange().ToSlice()
			p.DecRef()
			if want := g.data[off : off+g.mss]; !bytes.Equal(payload, want) {
				t.Fatalf("restored payload at +%d = %x, want %x", off, payload, want)
			}
		}
		sendRestoredGSOAck(g.Context, restored, link, len(g.data))
		if got, want := restored.Stats().TCP.Timeouts.Value()-before, uint64(0); got != want {
			t.Fatalf("restored timeouts = %d, want %d", got, want)
		}
	})
}

func TestRefusedWriteBehindFlight(t *testing.T) {
	g := newGSOWriteTest(t)
	g.write(g.data[:g.mss])
	g.ReceiveAndCheckPacket(g.data, 0, g.mss)
	g.refuse.Store(true)
	g.write(g.data[g.mss:])
	g.checkRefusal()
	g.refuse.Store(false)
	g.drainSuffix(0)
}

func TestACKForRefusedRetransmission(t *testing.T) {
	g := newGSOWriteTest(t)
	g.write(g.data[:g.mss])
	g.ReceiveAndCheckPacket(g.data, 0, g.mss)
	g.refuse.Store(true)
	g.advanceRTO()
	if p := g.GetPacketNonBlocking(); p != nil {
		p.Release()
		t.Fatal("received the declined retransmission")
	}
	stats := g.Stack().Stats().TCP
	if got, want := stats.Retransmits.Value(), uint64(1); got != want {
		t.Fatalf("retransmission attempts = %d, want %d", got, want)
	}
	if got, want := stats.SegmentSendErrors.Value(), uint64(1); got != want {
		t.Fatalf("route errors = %d, want %d", got, want)
	}
	if got, want := stats.Timeouts.Value(), uint64(1); got != want {
		t.Fatalf("timeouts = %d, want %d", got, want)
	}
	g.ack(0, g.mss, 1)
	g.refuse.Store(false)
	// The original packet reached the peer and can still be acknowledged.
	g.ack(g.mss, g.mss, 0)
}
