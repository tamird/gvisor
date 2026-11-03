// Copyright 2018 The gVisor Authors.
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

package forwarder_test

import (
	"os"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/atomicbitops"
	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/refs"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/checker"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp/test/e2e"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp/testing/context"
	"gvisor.dev/gvisor/pkg/waiter"
)

func TestForwarderSendMSSLessThanMTU(t *testing.T) {
	const maxPayload = 100
	const mtu = 1200
	c := context.New(t, mtu)
	defer c.Cleanup()

	s := c.Stack()
	ch := make(chan tcpip.Error, 1)
	f := tcp.NewForwarder(s, 65536, 10, func(r *tcp.ForwarderRequest) {
		var err tcpip.Error
		c.EP, err = r.CreateEndpoint(&c.WQ)
		ch <- err
		close(ch)
		r.Complete(false)
	})
	s.SetTransportProtocolHandler(tcp.ProtocolNumber, f.HandlePacket)

	// Do 3-way handshake.
	c.PassiveConnect(maxPayload, -1, header.TCPSynOptions{MSS: mtu - header.IPv4MinimumSize - header.TCPMinimumSize})

	// Wait for connection to be available.
	select {
	case err := <-ch:
		if err != nil {
			t.Fatalf("Error creating endpoint: %s", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatalf("Timed out waiting for connection")
	}

	// Check that data gets properly segmented.
	e2e.CheckBrokenUpWrite(t, c, maxPayload)
}

func TestForwarderDoesNotRejectECNFlags(t *testing.T) {
	testCases := []struct {
		name  string
		flags header.TCPFlags
	}{
		{name: "non-setup ECN SYN w/ ECE", flags: header.TCPFlagEce},
		{name: "non-setup ECN SYN w/ CWR", flags: header.TCPFlagCwr},
		{name: "setup ECN SYN", flags: header.TCPFlagEce | header.TCPFlagCwr},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			const maxPayload = 100
			const mtu = 1200
			c := context.New(t, mtu)
			defer c.Cleanup()

			s := c.Stack()
			ch := make(chan tcpip.Error, 1)
			f := tcp.NewForwarder(s, 65536, 10, func(r *tcp.ForwarderRequest) {
				var err tcpip.Error
				c.EP, err = r.CreateEndpoint(&c.WQ)
				ch <- err
				close(ch)
				r.Complete(false)
			})
			s.SetTransportProtocolHandler(tcp.ProtocolNumber, f.HandlePacket)

			// Do 3-way handshake.
			c.PassiveConnect(maxPayload, -1, header.TCPSynOptions{MSS: mtu - header.IPv4MinimumSize - header.TCPMinimumSize, Flags: tc.flags})

			// Wait for connection to be available.
			select {
			case err := <-ch:
				if err != nil {
					t.Fatalf("Error creating endpoint: %s", err)
				}
			case <-time.After(2 * time.Second):
				t.Fatalf("Timed out waiting for connection")
			}
		})
	}
}

func TestForwarderFailedConnect(t *testing.T) {
	const mtu = 1200
	c := context.New(t, mtu)
	defer c.Cleanup()

	s := c.Stack()
	ch := make(chan tcpip.Error, 1)
	f := tcp.NewForwarder(s, 65536, 10, func(r *tcp.ForwarderRequest) {
		var err tcpip.Error
		c.EP, err = r.CreateEndpoint(&c.WQ)
		ch <- err
		close(ch)
		r.Complete(false)
	})
	s.SetTransportProtocolHandler(tcp.ProtocolNumber, f.HandlePacket)

	// Initiate a connection that will be forwarded by the Forwarder.
	// Send a SYN request.
	iss := seqnum.Value(context.TestInitialSequenceNumber)
	c.SendPacket(nil, &context.Headers{
		SrcPort: context.TestPort,
		DstPort: context.StackPort,
		Flags:   header.TCPFlagSyn,
		SeqNum:  iss,
		RcvWnd:  30000,
	})

	// Receive the SYN-ACK reply. Make sure MSS and other expected options
	// are present.
	v := c.GetPacket()
	defer v.Release()
	tcp := header.TCP(header.IPv4(v.AsSlice()).Payload())
	c.IRS = seqnum.Value(tcp.SequenceNumber())

	tcpCheckers := []checker.TransportChecker{
		checker.SrcPort(context.StackPort),
		checker.DstPort(context.TestPort),
		checker.TCPFlags(header.TCPFlagAck | header.TCPFlagSyn),
		checker.TCPAckNum(uint32(iss) + 1),
	}
	checker.IPv4(t, v, checker.TCP(tcpCheckers...))

	// Now send an active RST to abort the handshake.
	c.SendPacket(nil, &context.Headers{
		SrcPort: context.TestPort,
		DstPort: context.StackPort,
		Flags:   header.TCPFlagRst,
		SeqNum:  iss + 1,
		RcvWnd:  0,
	})

	// Wait for connect to fail.
	select {
	case err := <-ch:
		if err == nil {
			t.Fatalf("endpoint creation should have failed")
		}
	case <-time.After(2 * time.Second):
		t.Fatalf("Timed out waiting for connection to fail")
	}
}

func TestForwarderReopenAfterTimeWait(t *testing.T) {
	c := context.New(t, e2e.DefaultMTU)
	defer c.Cleanup()

	ch, iss := forwardedTimeWait(t, c)

	// The 4-tuple is now in TIME_WAIT. Per RFC 1122, a new SYN with a
	// sequence number higher than anything seen on the old connection
	// should be allowed to reopen the connection. Since this stack only
	// has a tcp.Forwarder and no bound listening endpoint, the new SYN
	// must reach the forwarder just like a brand new connection would.
	newISS := iss.Add(3)
	c.SendPacket(nil, &context.Headers{
		SrcPort: context.TestPort,
		DstPort: context.StackPort,
		Flags:   header.TCPFlagSyn,
		SeqNum:  newISS,
		RcvWnd:  30000,
	})

	b := c.GetPacket()
	defer b.Release()
	tcpHdr := header.TCP(header.IPv4(b.AsSlice()).Payload())
	c.IRS = seqnum.Value(tcpHdr.SequenceNumber())
	checker.IPv4(t, b, checker.TCP(
		checker.SrcPort(context.StackPort),
		checker.DstPort(context.TestPort),
		checker.TCPFlags(header.TCPFlagSyn|header.TCPFlagAck),
		checker.TCPAckNum(uint32(newISS)+1)))

	c.SendPacket(nil, &context.Headers{
		SrcPort: context.TestPort,
		DstPort: context.StackPort,
		Flags:   header.TCPFlagAck,
		SeqNum:  newISS + 1,
		AckNum:  c.IRS + 1,
	})

	select {
	case err := <-ch:
		if err != nil {
			t.Fatalf("Error creating endpoint for reopened connection: %s", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatalf("Timed out waiting for reopened connection")
	}
}

// forwardedTimeWait does a 3-way handshake through a tcp.Forwarder on c's
// stack and closes the resulting endpoint, exchanging FINs until the forwarded
// endpoint sits in TIME_WAIT. It returns the forwarder's result channel and
// the peer's initial sequence number; the stack's is left in c.IRS.
func forwardedTimeWait(t *testing.T, c *context.Context) (chan tcpip.Error, seqnum.Value) {
	t.Helper()

	iss := seqnum.Value(context.TestInitialSequenceNumber)
	return forwardedTimeWaitAt(t, c, iss), iss
}

func forwardedTimeWaitAt(t *testing.T, c *context.Context, iss seqnum.Value) chan tcpip.Error {
	t.Helper()
	s := c.Stack()
	// Buffered so the handler for either connection can send its result
	// without waiting on the test to catch up.
	ch := make(chan tcpip.Error, 2)
	f := tcp.NewForwarder(s, 65536, 10, func(r *tcp.ForwarderRequest) {
		var err tcpip.Error
		c.EP, err = r.CreateEndpoint(&c.WQ)
		ch <- err
		r.Complete(false)
	})
	s.SetTransportProtocolHandler(tcp.ProtocolNumber, f.HandlePacket)

	c.SendPacket(nil, &context.Headers{
		SrcPort: context.TestPort,
		DstPort: context.StackPort,
		Flags:   header.TCPFlagSyn,
		SeqNum:  iss,
		RcvWnd:  30000,
	})

	b := c.GetPacket()
	defer b.Release()
	tcpHdr := header.TCP(header.IPv4(b.AsSlice()).Payload())
	c.IRS = seqnum.Value(tcpHdr.SequenceNumber())

	c.SendPacket(nil, &context.Headers{
		SrcPort: context.TestPort,
		DstPort: context.StackPort,
		Flags:   header.TCPFlagAck,
		SeqNum:  iss + 1,
		AckNum:  c.IRS + 1,
	})

	select {
	case err := <-ch:
		if err != nil {
			t.Fatalf("Error creating endpoint: %s", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatalf("Timed out waiting for connection")
	}

	c.EP.Close()

	v := c.GetPacket()
	defer v.Release()
	checker.IPv4(t, v, checker.TCP(
		checker.SrcPort(context.StackPort),
		checker.DstPort(context.TestPort),
		checker.TCPSeqNum(uint32(c.IRS+1)),
		checker.TCPAckNum(uint32(iss)+1),
		checker.TCPFlags(header.TCPFlagFin|header.TCPFlagAck)))

	// Ack our FIN and send our own, which puts the forwarded endpoint into
	// TIME_WAIT once we ack it below.
	c.SendPacket(nil, &context.Headers{
		SrcPort: context.TestPort,
		DstPort: context.StackPort,
		Flags:   header.TCPFlagAck | header.TCPFlagFin,
		SeqNum:  iss + 1,
		AckNum:  c.IRS + 2,
	})

	v = c.GetPacket()
	defer v.Release()
	checker.IPv4(t, v, checker.TCP(
		checker.SrcPort(context.StackPort),
		checker.DstPort(context.TestPort),
		checker.TCPSeqNum(uint32(c.IRS+2)),
		checker.TCPAckNum(uint32(iss)+2),
		checker.TCPFlags(header.TCPFlagAck)))

	return ch
}

// TestForwarderTimeWaitSynAckKeepsEndpoint checks that a SYN-ACK arriving
// during TIME_WAIT is not taken for a reopening SYN: it is acknowledged with
// the old connection's sequence numbers, as Linux does, and the TIME_WAIT
// endpoint stays to answer a repeated FIN the same way. A SYN without ACK
// still reaches the forwarder afterwards.
func TestForwarderTimeWaitSynAckKeepsEndpoint(t *testing.T) {
	c := context.New(t, e2e.DefaultMTU)
	defer c.Cleanup()

	ch, iss := forwardedTimeWait(t, c)

	// A SYN-ACK with a sequence number higher than anything seen on the old
	// connection is still a segment of the old incarnation, not a reopening
	// SYN: the endpoint stays in TIME_WAIT and ACKs it with its own numbers.
	newISS := iss.Add(3)
	c.SendPacket(nil, &context.Headers{
		SrcPort: context.TestPort,
		DstPort: context.StackPort,
		Flags:   header.TCPFlagSyn | header.TCPFlagAck,
		SeqNum:  newISS,
		AckNum:  c.IRS + 100,
		RcvWnd:  30000,
	})

	v := c.GetPacket()
	defer v.Release()
	checker.IPv4(t, v, checker.TCP(
		checker.SrcPort(context.StackPort),
		checker.DstPort(context.TestPort),
		checker.TCPSeqNum(uint32(c.IRS+2)),
		checker.TCPAckNum(uint32(iss)+2),
		checker.TCPFlags(header.TCPFlagAck)))

	select {
	case err := <-ch:
		t.Fatalf("forwarder unexpectedly created an endpoint for a SYN-ACK: %s", err)
	case <-time.After(100 * time.Millisecond):
	}

	// The old connection's FIN, repeated as if our final ACK was lost, is
	// still acknowledged by the TIME_WAIT endpoint.
	c.SendPacket(nil, &context.Headers{
		SrcPort: context.TestPort,
		DstPort: context.StackPort,
		Flags:   header.TCPFlagAck | header.TCPFlagFin,
		SeqNum:  iss + 1,
		AckNum:  c.IRS + 2,
	})

	v = c.GetPacket()
	defer v.Release()
	checker.IPv4(t, v, checker.TCP(
		checker.SrcPort(context.StackPort),
		checker.DstPort(context.TestPort),
		checker.TCPSeqNum(uint32(c.IRS+2)),
		checker.TCPAckNum(uint32(iss)+2),
		checker.TCPFlags(header.TCPFlagAck)))

	// A genuine reopening SYN still goes to the forwarder.
	c.SendPacket(nil, &context.Headers{
		SrcPort: context.TestPort,
		DstPort: context.StackPort,
		Flags:   header.TCPFlagSyn,
		SeqNum:  newISS,
		RcvWnd:  30000,
	})

	b := c.GetPacket()
	defer b.Release()
	checker.IPv4(t, b, checker.TCP(
		checker.SrcPort(context.StackPort),
		checker.DstPort(context.TestPort),
		checker.TCPFlags(header.TCPFlagSyn|header.TCPFlagAck),
		checker.TCPAckNum(uint32(newISS)+1)))
}

// TestForwarderTimeWaitDeclinedSynGetsReset checks that a reopening SYN the
// stack's default handler declines to claim is answered with a RST, the way
// the normal (non-TIME_WAIT) delivery path answers a SYN nobody claims,
// rather than dropped silently.
func TestForwarderTimeWaitDeclinedSynGetsReset(t *testing.T) {
	c := context.New(t, e2e.DefaultMTU)
	defer c.Cleanup()

	_, iss := forwardedTimeWait(t, c)

	// Replace the forwarder with a default handler that claims nothing.
	c.Stack().SetTransportProtocolHandler(tcp.ProtocolNumber, func(stack.TransportEndpointID, *stack.PacketBuffer) bool {
		return false
	})

	newISS := iss.Add(3)
	c.SendPacket(nil, &context.Headers{
		SrcPort: context.TestPort,
		DstPort: context.StackPort,
		Flags:   header.TCPFlagSyn,
		SeqNum:  newISS,
		RcvWnd:  30000,
	})

	v := c.GetPacket()
	defer v.Release()
	checker.IPv4(t, v, checker.TCP(
		checker.SrcPort(context.StackPort),
		checker.DstPort(context.TestPort),
		checker.TCPFlags(header.TCPFlagRst|header.TCPFlagAck),
		checker.TCPSeqNum(0),
		checker.TCPAckNum(uint32(newISS)+1)))
}

func TestForwarderDroppedStats(t *testing.T) {
	const maxPayload = 100
	const mtu = 1200
	c := context.New(t, mtu)
	defer c.Cleanup()

	const maxInFlight = 2
	iters := atomicbitops.FromInt64(maxInFlight)
	s := c.Stack()
	checkedStats := make(chan struct{})
	done := make(chan struct{})
	f := tcp.NewForwarder(s, 65536, maxInFlight, func(r *tcp.ForwarderRequest) {
		<-checkedStats
		// Complete all requests without doing anything
		r.Complete(false)
		if iter := iters.Add(-1); iter == 0 {
			close(done)
		}
	})
	s.SetTransportProtocolHandler(tcp.ProtocolNumber, f.HandlePacket)

	for i := 0; i < maxInFlight+1; i++ {
		iss := seqnum.Value(context.TestInitialSequenceNumber + i)
		c.SendPacket(nil, &context.Headers{
			SrcPort: uint16(context.TestPort + i),
			DstPort: context.StackPort,
			Flags:   header.TCPFlagSyn,
			SeqNum:  iss,
			RcvWnd:  30000,
		})
	}

	// Verify that we got one ignored packet.
	if curr := s.Stats().TCP.ForwardMaxInFlightDrop.Value(); curr != 1 {
		t.Errorf("Expected one dropped connection, but got %d", curr)
	}
	close(checkedStats)
	<-done
}

// TestQueuedForwarderReconnect uses a second TCP stack as the connecting peer.
func TestQueuedForwarderReconnect(t *testing.T) {
	for _, tc := range []struct {
		name   string
		copies int
	}{{"single", 1}, {"duplicated", 2}} {
		t.Run(tc.name, func(t *testing.T) {
			server := context.New(t, e2e.DefaultMTU)
			defer server.Cleanup()
			client := context.NewWithOpts(t, context.Options{EnableV4: true, MTU: e2e.DefaultMTU, Clock: faketime.NewManualClock()})
			defer client.Cleanup()

			// Both test stacks own StackAddr and send to TestAddr. Swap those
			// two IPv4 addresses between them. This preserves both checksums
			// and delivers the entire TCP segment unchanged.
			read := func(c *context.Context) []byte {
				t.Helper()
				v := c.GetPacket()
				defer v.Release()
				b := append([]byte(nil), v.AsSlice()...)
				ip := header.IPv4(b)
				src, dst := ip.SourceAddress(), ip.DestinationAddress()
				ip.SetSourceAddress(dst)
				ip.SetDestinationAddress(src)
				return b
			}
			deliver := func(c *context.Context, b []byte) {
				c.SendSegment(buffer.MakeWithData(b))
			}
			tcpOf := func(b []byte) header.TCP { return header.TCP(header.IPv4(b).Payload()) }

			client.Create(-1)
			if err := client.EP.Bind(tcpip.FullAddress{Addr: context.StackAddr, Port: context.TestPort}); err != nil {
				t.Fatalf("bind: %v", err)
			}
			we, ready := waiter.NewChannelEntry(waiter.WritableEvents)
			client.WQ.EventRegister(&we)
			defer client.WQ.EventUnregister(&we)
			if _, ok := client.EP.Connect(tcpip.FullAddress{Addr: context.TestAddr, Port: context.StackPort}).(*tcpip.ErrConnectStarted); !ok {
				t.Fatal("connect did not start")
			}
			syn := read(client)
			h := tcpOf(syn)
			if h.Flags() != header.TCPFlagSyn || h.SourcePort() != context.TestPort || h.DestinationPort() != context.StackPort {
				t.Fatalf("unexpected opening segment: flags=%v ports=%d→%d", h.Flags(), h.SourcePort(), h.DestinationPort())
			}
			// The original connection used sequence space just before the
			// real client's ISN, as in TestForwarderReopenAfterTimeWait.
			ch := forwardedTimeWaitAt(t, server, seqnum.Value(h.SequenceNumber()-3))
			server.Stack().Pause()
			for i := 0; i < tc.copies; i++ {
				deliver(server, syn)
			}
			server.Stack().Resume()
			reply := read(server)
			r := tcpOf(reply)
			if r.DestinationPort() != h.SourcePort() || r.SourcePort() != h.DestinationPort() || r.AckNumber() != h.SequenceNumber()+1 {
				t.Fatalf("reply did not acknowledge client's SYN: %v", r.Flags())
			}
			deliver(client, reply)
			select {
			case <-ready:
			case <-time.After(5 * time.Second):
				t.Fatal("client connect did not complete")
			}
			err := client.EP.LastError()
			var outcome string
			switch r.Flags() {
			case header.TCPFlagSyn | header.TCPFlagAck:
				if err != nil || tcp.EndpointState(client.EP.State()) != tcp.StateEstablished {
					t.Fatalf("SYN-ACK: client state=%v err=%v", client.EP.State(), err)
				}
				outcome = "connected"
			case header.TCPFlagRst | header.TCPFlagAck:
				if _, ok := err.(*tcpip.ErrConnectionRefused); !ok || tc.copies == 1 {
					t.Fatalf("RST before handshake: client err=%v copies=%d", err, tc.copies)
				}
				outcome = "refused"
				// Let the closed client answer the forwarder's subsequent
				// SYN-ACK, so CreateEndpoint gets the real peer response.
				late := read(server)
				l := tcpOf(late)
				if l.Flags() != header.TCPFlagSyn|header.TCPFlagAck || l.AckNumber() != h.SequenceNumber()+1 {
					t.Fatalf("unexpected late response: %v", l.Flags())
				}
				deliver(client, late)
			default:
				t.Fatalf("unexpected first response: %v", r.Flags())
			}
			feedback := read(client)
			final := tcpOf(feedback).Flags()
			if outcome == "connected" && final != header.TCPFlagAck || outcome == "refused" && final != header.TCPFlagRst {
				t.Fatalf("client %s, unexpected reply: %v", outcome, final)
			}
			deliver(server, feedback)
			select {
			case serverErr := <-ch:
				if outcome == "connected" && serverErr != nil || outcome == "refused" && serverErr == nil {
					t.Fatalf("client %s, forwarder error = %v", outcome, serverErr)
				}
				t.Logf("RECONNECT copies=%d first=%d client=%s peer_reply=%d forwarder_error=%T", tc.copies, r.Flags(), outcome, final, serverErr)
			case <-time.After(5 * time.Second):
				t.Fatal("forwarder did not complete")
			}
		})
	}
}

func TestMain(m *testing.M) {
	refs.SetLeakMode(refs.LeaksPanic)
	code := m.Run()
	// Allow TCP async work to complete to avoid false reports of leaks.
	// TODO(gvisor.dev/issue/5940): Use fake clock in tests.
	time.Sleep(1 * time.Second)
	refs.DoLeakCheck()
	os.Exit(code)
}
