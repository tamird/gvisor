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
	"context"
	"reflect"
	"testing"
	"time"
	"unsafe"

	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/checker"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/network/arp"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv6"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp/test/e2e"
	testcontext "gvisor.dev/gvisor/pkg/tcpip/transport/tcp/testing/context"
	"gvisor.dev/gvisor/pkg/waiter"
)

func TestTCPMigration(t *testing.T) {
	testCases := []struct {
		name                 string
		allowMigration       bool
		removeConf           bool
		hasRouteAfterRestore bool
		wantOriginalState    tcp.EndpointState
		wantRestoredState    tcp.EndpointState
		expectRestored       bool
		smallerMTU           bool
	}{
		{
			name:                 "MigrationAllowed_RemoveConf_RouteExists",
			allowMigration:       true,
			removeConf:           true,
			hasRouteAfterRestore: true,
			wantOriginalState:    tcp.StateEstablished,
			wantRestoredState:    tcp.StateEstablished,
			expectRestored:       true,
		},
		{
			name:                 "MigrationAllowed_SmallerMTUWithOutstandingPacket",
			allowMigration:       true,
			removeConf:           true,
			hasRouteAfterRestore: true,
			wantOriginalState:    tcp.StateEstablished,
			wantRestoredState:    tcp.StateEstablished,
			expectRestored:       true,
			smallerMTU:           true,
		},
		{
			name:                 "MigrationDisabled_RemoveConf",
			allowMigration:       false,
			removeConf:           true,
			hasRouteAfterRestore: true,
			wantOriginalState:    tcp.StateError,
			wantRestoredState:    tcp.StateError,
			expectRestored:       true,
		},
		{
			name:                 "MigrationAllowed_RemoveConf_NoRoute",
			allowMigration:       true,
			removeConf:           true,
			hasRouteAfterRestore: false,
			wantOriginalState:    tcp.StateEstablished,
			wantRestoredState:    tcp.StateError,
			expectRestored:       true,
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			// Create stack.
			opts := testcontext.Options{EnableV4: true, EnableV6: true, MTU: e2e.DefaultMTU}
			if tc.smallerMTU {
				opts.Clock = faketime.NewManualClock()
			}
			c := testcontext.NewWithOpts(t, opts)
			defer c.Cleanup()

			c.Stack().SetAllowLiveTCPMigration(tc.allowMigration)
			c.Stack().SetRemoveConf(tc.removeConf)

			// Establish connection.
			const peerMSS = 1200
			var options []byte
			if tc.smallerMTU {
				options = make([]byte, 4)
				header.EncodeMSSOption(peerMSS, options)
			}
			c.CreateConnectedWithRawOptions(testcontext.TestInitialSequenceNumber, 30000, -1 /* epRcvBuf */, options)
			var oldEnd seqnum.Value
			if tc.smallerMTU {
				data := bytes.Repeat([]byte{0x42}, peerMSS)
				if n, err := c.EP.Write(bytes.NewReader(data), tcpip.WriteOptions{}); err != nil || n != peerMSS {
					t.Fatalf("Write = %d, %v", n, err)
				}
				p := c.GetPacket()
				defer p.Release()
				h := header.TCP(header.IPv4(p.AsSlice()).Payload())
				if !bytes.Equal(h.Payload(), data) {
					t.Fatal("old route did not send the whole payload in one packet")
				}
				oldEnd = seqnum.Value(h.SequenceNumber()).Add(peerMSS)
			}

			if got, want := tcp.EndpointState(c.EP.State()), tcp.StateEstablished; got != want {
				t.Fatalf("Unexpected endpoint state: want %v, got %v", want, got)
			}

			// Save the stack.
			var buf bytes.Buffer
			saveStats, err := state.Save(context.Background(), &buf, c.Stack())
			if err != nil {
				t.Fatalf("Save failed: %v", err)
			}
			t.Logf("Save stats:\n%s", saveStats.String())

			if got, want := tcp.EndpointState(c.EP.State()), tc.wantOriginalState; got != want {
				t.Fatalf("Unexpected original endpoint state after save: want %v, got %v", want, got)
			}

			// Restore the stack.
			restoredStack := stack.New(stack.Options{
				TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol},
				NetworkProtocols:   []stack.NetworkProtocolFactory{ipv4.NewProtocol, ipv6.NewProtocol},
			})
			restoredStack.SetAllowLiveTCPMigration(tc.allowMigration)
			defer restoredStack.Destroy()

			loadStats, err := state.Load(context.Background(), bytes.NewReader(buf.Bytes()), restoredStack)
			if err != nil {
				t.Fatalf("Load failed: %v", err)
			}
			t.Logf("Load stats:\n%s", loadStats.String())

			var link *channel.Endpoint
			if tc.hasRouteAfterRestore {
				// Recreate the NIC 1 with same IP.
				mtu := uint32(e2e.DefaultMTU)
				if tc.smallerMTU {
					mtu = header.IPv4MinimumSize + header.TCPMinimumSize + peerMSS - 4
				}
				link = channel.New(1000, mtu, "")
				if err := restoredStack.CreateNIC(1, link); err != nil {
					t.Fatalf("CreateNIC failed: %v", err)
				}
				if err := restoredStack.AddProtocolAddress(1, tcpip.ProtocolAddress{
					Protocol:          header.IPv4ProtocolNumber,
					AddressWithPrefix: testcontext.StackAddrWithPrefix,
				}, stack.AddressProperties{}); err != nil {
					t.Fatalf("AddProtocolAddress failed: %v", err)
				}
				restoredStack.SetRouteTable([]tcpip.Route{
					{
						Destination: header.IPv4EmptySubnet,
						NIC:         1,
					},
				})
			}

			stackType := reflect.TypeOf(restoredStack).Elem()
			sf, ok := stackType.FieldByName("restoredEndpoints")
			if !ok {
				t.Fatalf("Field restoredEndpoints not found")
			}
			offset := sf.Offset
			ptr := unsafe.Pointer(uintptr(unsafe.Pointer(restoredStack)) + offset)
			restoredEndpointsPtr := (*[]stack.RestoredEndpoint)(ptr)
			restoredEndpoints := *restoredEndpointsPtr

			var restoredEP tcpip.Endpoint
			if tc.expectRestored {
				if len(restoredEndpoints) != 1 {
					t.Fatalf("Expected 1 restored endpoint, got %d", len(restoredEndpoints))
				}
				var ok bool
				restoredEP, ok = restoredEndpoints[0].(tcpip.Endpoint)
				if !ok {
					t.Fatalf("Restored endpoint does not implement tcpip.Endpoint")
				}
			} else {
				if len(restoredEndpoints) != 0 {
					t.Fatalf("Expected 0 restored endpoints, got %d", len(restoredEndpoints))
				}
			}

			// Now call Restore.
			restoredStack.Restore()

			if tc.expectRestored {
				got, want := tcp.EndpointState(restoredEP.State()), tc.wantRestoredState
				t.Logf("Subtest %s: got state: %v, want state: %v", tc.name, got, want)
				if got != want {
					t.Fatalf("Unexpected restored endpoint state: want %v, got %v", want, got)
				}
			}
			if tc.smallerMTU {
				// One ACK covers the one packet observed on the old route,
				// even though its payload exceeds the new MSS.
				var before, after tcpip.TCPInfoOption
				if err := restoredEP.GetSockOpt(&before); err != nil || before.SndMss != peerMSS-4 || before.SndCwnd+2 >= before.SndSsthresh {
					t.Fatalf("unexpected restored TCP_INFO: %+v, %v", before, err)
				}
				p := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: c.BuildSegment([]byte{1}, &testcontext.Headers{
					SrcPort: testcontext.TestPort, DstPort: c.Port, SeqNum: testcontext.TestInitialSequenceNumber + 1,
					AckNum: oldEnd, Flags: header.TCPFlagAck | header.TCPFlagPsh, RcvWnd: 30000,
				})})
				link.InjectInbound(ipv4.ProtocolNumber, p)
				p.DecRef()
				deadline := time.Now().Add(5 * time.Second)
				for restoredEP.Readiness(waiter.ReadableEvents) == 0 && time.Now().Before(deadline) {
					time.Sleep(time.Millisecond)
				}
				var got bytes.Buffer
				if _, err := restoredEP.Read(&got, tcpip.ReadOptions{}); err != nil || !bytes.Equal(got.Bytes(), []byte{1}) {
					t.Fatalf("read ACK's data = %v, %v", got.Bytes(), err)
				}
				if err := restoredEP.GetSockOpt(&after); err != nil || after.SndCwnd != before.SndCwnd+1 {
					t.Fatalf("ACK of one old packet at smaller MSS: cwnd %d -> %d, want %d, %v", before.SndCwnd, after.SndCwnd, before.SndCwnd+1, err)
				}
			}
		})
	}
}

// TestTCPMigrationMTUAckCredit observes a cumulative ACK for data sent on
// the original route. Both endpoints use ordinary IPv4 and TCP headers.
func TestTCPMigrationMTUAckCredit(t *testing.T) {
	const sourceMTU = 1500
	const peerMSS = 1200
	for _, tc := range []struct {
		name        string
		mtu         uint32
		wantMSS     int
		two, repair bool
		acks        []int
	}{
		{"same", sourceMTU, peerMSS, false, false, []int{1200}},
		{"smaller", 1236, 1196, false, false, []int{1200}},
		{"partial", 1236, 1196, false, false, []int{4, 1196, 1196, 1200, 1200}},
		{"two", 1236, 1196, true, false, []int{1204, 1204}},
		{"repair", 1236, 1196, false, true, []int{1196, 1200, 1200}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := testcontext.NewWithOpts(t, testcontext.Options{
				EnableV4: true, MTU: sourceMTU, Clock: faketime.NewManualClock(),
			})
			defer c.Cleanup()
			c.Stack().SetAllowLiveTCPMigration(true)
			c.Stack().SetRemoveConf(true)
			var options [4]byte
			header.EncodeMSSOption(peerMSS, options[:])
			c.CreateConnectedWithRawOptions(testcontext.TestInitialSequenceNumber, 30000, -1, options[:])
			checkRoute := func(s *stack.Stack, ep tcpip.Endpoint, mtu uint32) {
				t.Helper()
				r, err := s.FindRoute(1, testcontext.StackAddr, testcontext.TestAddr, ipv4.ProtocolNumber, false)
				if err != nil {
					t.Fatal(err)
				}
				defer r.Release()
				nic, err := s.GetNICByID(r.OutgoingNIC())
				if err != nil || nic.GetExperimentIPOptionEnabled() || ep.SocketOptions().GetExperimentOptionValue() != 0 ||
					r.HasHostGSOCapability() || r.HasGVisorGSOCapability() || r.MTU() != mtu-header.IPv4MinimumSize {
					t.Fatalf("expected a plain IPv4 route on the %d-byte NIC", mtu)
				}
			}
			checkRoute(c.Stack(), c.EP, sourceMTU)
			cc := tcpip.CongestionControlOption("reno")
			if err := c.EP.SetSockOpt(&cc); err != nil {
				t.Fatal(err)
			}
			send := func(ep tcpip.Endpoint, data []byte) {
				t.Helper()
				if n, err := ep.Write(bytes.NewReader(data), tcpip.WriteOptions{}); err != nil || n != int64(len(data)) {
					t.Fatalf("Write = %d, %v", n, err)
				}
			}
			var initial tcpip.TCPInfoOption
			if err := c.EP.GetSockOpt(&initial); err != nil || initial.SndMss != peerMSS {
				t.Fatalf("initial snd_mss = %d, %v", initial.SndMss, err)
			}
			data := bytes.Repeat([]byte{0xa7}, peerMSS)
			send(c.EP, data)
			oldPacket := c.GetPacket()
			defer oldPacket.Release()
			oldIP := header.IPv4(oldPacket.AsSlice())
			oldTCP := header.TCP(oldIP.Payload())
			if oldIP.HeaderLength() != header.IPv4MinimumSize || oldTCP.DataOffset() != header.TCPMinimumSize || !bytes.Equal(oldTCP.Payload(), data) {
				t.Fatal("previous route did not emit one complete 1200-byte payload with base headers")
			}
			first := seqnum.Value(oldTCP.SequenceNumber())
			if tc.two {
				c.EP.SocketOptions().SetDelayOption(false)
				tail := []byte{1, 2, 3, 4}
				send(c.EP, tail)
				p := c.GetPacket()
				defer p.Release()
				h := header.TCP(header.IPv4(p.AsSlice()).Payload())
				if h.SequenceNumber() != uint32(first.Add(peerMSS)) || !bytes.Equal(h.Payload(), tail) {
					t.Fatal("second original transmission missing")
				}
			}

			var image bytes.Buffer
			if _, err := state.Save(context.Background(), &image, c.Stack()); err != nil {
				t.Fatal(err)
			}
			c.Stack().Resume()
			s := stack.New(stack.Options{
				TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol},
				NetworkProtocols:   []stack.NetworkProtocolFactory{ipv4.NewProtocol},
			})
			defer s.Destroy()
			s.SetAllowLiveTCPMigration(true)
			if _, err := state.Load(context.Background(), &image, s); err != nil {
				t.Fatal(err)
			}
			link := channel.New(1000, tc.mtu, "")
			if err := s.CreateNIC(1, link); err != nil {
				t.Fatal(err)
			}
			if err := s.AddProtocolAddress(1, tcpip.ProtocolAddress{
				Protocol: header.IPv4ProtocolNumber, AddressWithPrefix: testcontext.StackAddrWithPrefix,
			}, stack.AddressProperties{}); err != nil {
				t.Fatal(err)
			}
			s.SetRouteTable([]tcpip.Route{{Destination: header.IPv4EmptySubnet, NIC: 1}})
			s.Restore()
			tcpip.AsyncLoading.Wait()
			eps := s.RegisteredEndpoints()
			if len(eps) != 1 {
				t.Fatalf("restored %d registered endpoints", len(eps))
			}
			ep := eps[0].(tcpip.Endpoint)
			checkRoute(s, ep, tc.mtu)
			if state := tcp.EndpointState(ep.State()); state != tcp.StateEstablished {
				t.Fatalf("restored state = %s", state)
			}
			info := func() tcpip.TCPInfoOption {
				t.Helper()
				var x tcpip.TCPInfoOption
				if err := ep.GetSockOpt(&x); err != nil || int(x.SndMss) != tc.wantMSS {
					t.Fatalf("TCP_INFO: %+v %v", x, err)
				}
				return x
			}
			readData := func(want []byte, offset int) {
				t.Helper()
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				for {
					p := link.ReadContext(ctx)
					if p == nil {
						t.Fatalf("no data at %d", offset)
					}
					v := p.ToView()
					p.DecRef()
					ip := header.IPv4(v.AsSlice())
					h := header.TCP(ip.Payload())
					n := len(h.Payload())
					ok := h.SequenceNumber() == uint32(first.Add(seqnum.Size(offset))) &&
						ip.HeaderLength() == header.IPv4MinimumSize && h.DataOffset() == header.TCPMinimumSize &&
						bytes.Equal(h.Payload(), want)
					v.Release()
					if n == 0 {
						continue
					}
					if !ok {
						t.Fatalf("unexpected %d-byte data packet, wanted %d bytes at %d", n, len(want), offset)
					}
					return
				}
			}
			before := info()
			total := peerMSS
			if tc.two {
				total += 4
			}
			if before.SndCwnd == 0 || before.SndCwnd+3 >= before.SndSsthresh || before.SACKPermitted {
				t.Fatalf("unexpected initial Reno state %+v", before)
			}
			next := seqnum.Value(testcontext.TestInitialSequenceNumber + 1)
			sendACK := func(offset int, value byte) {
				t.Helper()
				mark := []byte{value}
				p := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: c.BuildSegment(mark, &testcontext.Headers{
					SrcPort: testcontext.TestPort, DstPort: c.Port, SeqNum: next,
					AckNum: first.Add(seqnum.Size(offset)), Flags: header.TCPFlagAck | header.TCPFlagPsh, RcvWnd: 30000,
				})})
				link.InjectInbound(ipv4.ProtocolNumber, p)
				p.DecRef()
				deadline := time.Now().Add(5 * time.Second)
				for ep.Readiness(waiter.ReadableEvents) == 0 && time.Now().Before(deadline) {
					time.Sleep(time.Millisecond)
				}
				var got bytes.Buffer
				if _, err := ep.Read(&got, tcpip.ReadOptions{}); err != nil || !bytes.Equal(got.Bytes(), mark) {
					t.Fatalf("ACK %d: peer marker %x, %v", offset, got.Bytes(), err)
				}
				next++
			}
			if tc.repair {
				clock := s.Clock().(*faketime.ManualClock)
				// Only the old packet has left the sender. Withhold its ACK
				// until a real resend is seen on the smaller restored route.
				if before.RTO <= 0 || before.RTO > 10*time.Second {
					t.Fatalf("unexpected RTO: %s", before.RTO)
				}
				// Restore initialized the resend timer disabled. A non-advancing
				// ACK carrying a peer byte invokes postXmit and arms it without
				// acknowledging any of the 1200 outstanding data bytes.
				sendACK(0, 0x21)
				armed := info()
				if armed.SndCwnd != before.SndCwnd || armed.TotalRetrans != before.TotalRetrans || armed.RTO != before.RTO {
					t.Fatalf("arming ACK changed the original flight: before %+v, after %+v", before, armed)
				}
				clock.Advance(armed.RTO - time.Nanosecond)
				if info().TotalRetrans != armed.TotalRetrans || s.Stats().TCP.Timeouts.Value() != 0 {
					t.Fatal("repair before the armed timeout")
				}
				clock.Advance(time.Nanosecond)
				readData(data[:tc.wantMSS], 0)
				t.Logf("RTO_ARM offset=0 peer_byte=33 cwnd=%d delay_ns=%d", armed.SndCwnd, armed.RTO.Nanoseconds())
				now := info()
				if now.TotalRetrans <= before.TotalRetrans || now.SndCwnd != 1 || s.Stats().TCP.Timeouts.Value() != 1 {
					t.Fatalf("first retransmission did not follow one RTO: %+v", now)
				}
			}
			for i, offset := range tc.acks {
				if tc.repair && i == 1 {
					readData(data[tc.wantMSS:], tc.wantMSS)
				}
				prior := info()
				sendACK(offset, byte(0x31+i))
				now := info()
				t.Logf("ACK_STEP case=%s index=%d offset=%d before=%d after=%d retrans=%d", tc.name, i, offset, prior.SndCwnd, now.SndCwnd, now.TotalRetrans)
			}
			fresh := bytes.Repeat([]byte{0x5a}, 2*peerMSS)
			send(ep, fresh)
			readData(fresh[:tc.wantMSS], total)
			t.Logf("ACK_WIRE case=%s source_mtu=1500 mtu=%d first=1200 second=%t repair=%t fresh=%d timeouts=%d", tc.name, tc.mtu, tc.two, tc.repair, tc.wantMSS, s.Stats().TCP.Timeouts.Value())
		})
	}
}

// TestRestoreListenWithPreexistingConnection tests that a listening endpoint
// properly enters StateListen and accepts new connections after restore, even
// when the snapshot contained active or lingering connected TCP endpoints (e.g.
// from pre-checkpoint readiness probes) that are terminated upon restore
// without live migration.
func TestRestoreListenWithPreexistingConnection(t *testing.T) {
	acceptProbe := func(t *testing.T, c *testcontext.Context) tcpip.Endpoint {
		t.Helper()
		waitEntry, ch := waiter.NewChannelEntry(waiter.ReadableEvents)
		c.WQ.EventRegister(&waitEntry)
		defer c.WQ.EventUnregister(&waitEntry)
		ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
		defer cancel()
		for {
			ep, _, err := c.EP.Accept(nil)
			if err == nil {
				return ep
			}
			if _, ok := err.(*tcpip.ErrWouldBlock); !ok {
				t.Fatalf("Accept failed: %v", err)
			}
			select {
			case <-ch:
			case <-ctx.Done():
				t.Fatalf("waiting for accept: %v", ctx.Err())
			}
		}
	}

	testCases := []struct {
		name        string
		setupProbes func(t *testing.T, c *testcontext.Context) []tcpip.Endpoint
	}{
		{
			name: "EstablishedProbeConnection",
			setupProbes: func(t *testing.T, c *testcontext.Context) []tcpip.Endpoint {
				c.PassiveConnect(100, -1, header.TCPSynOptions{MSS: e2e.DefaultIPv4MSS})

				acceptedEP := acceptProbe(t, c)
				return []tcpip.Endpoint{acceptedEP}
			},
		},
		{
			name: "CloseWaitProbeConnection",
			setupProbes: func(t *testing.T, c *testcontext.Context) []tcpip.Endpoint {
				c.PassiveConnect(100, -1, header.TCPSynOptions{MSS: e2e.DefaultIPv4MSS})

				acceptedEP := acceptProbe(t, c)

				// Peer sends FIN to transition server-side accepted endpoint to StateCloseWait.
				c.SendPacket(nil, &testcontext.Headers{
					SrcPort: testcontext.TestPort,
					DstPort: testcontext.StackPort,
					Flags:   header.TCPFlagFin | header.TCPFlagAck,
					SeqNum:  seqnum.Value(testcontext.TestInitialSequenceNumber + 1),
					AckNum:  c.IRS + 1,
					RcvWnd:  30000,
				})

				// Drain the ACK from the server for the FIN.
				b := c.GetPacket()
				b.Release()

				if got, want := tcp.EndpointState(acceptedEP.State()), tcp.StateCloseWait; got != want {
					t.Fatalf("Unexpected probe endpoint state: want %v, got %v", want, got)
				}

				return []tcpip.Endpoint{acceptedEP}
			},
		},
		{
			name: "MultipleProbeConnections",
			setupProbes: func(t *testing.T, c *testcontext.Context) []tcpip.Endpoint {
				var acceptedEPs []tcpip.Endpoint
				for i := uint16(0); i < 3; i++ {
					srcPort := testcontext.TestPort + i
					iss := seqnum.Value(testcontext.TestInitialSequenceNumber + i*1000)

					// Send SYN.
					synOpts := make([]byte, header.TCPOptionsMaximumSize)
					optOffset := header.EncodeMSSOption(uint32(e2e.DefaultIPv4MSS), synOpts)
					padding := 4 - optOffset%4
					for j := optOffset; j < optOffset+padding; j++ {
						synOpts[j] = header.TCPOptionNOP
					}
					optOffset += padding

					c.SendPacket(nil, &testcontext.Headers{
						SrcPort: srcPort,
						DstPort: testcontext.StackPort,
						Flags:   header.TCPFlagSyn,
						SeqNum:  iss,
						RcvWnd:  30000,
						TCPOpts: synOpts[:optOffset],
					})

					// Receive SYN-ACK.
					b := c.GetPacket()
					tcpHdr := header.TCP(header.IPv4(b.AsSlice()).Payload())
					serverISS := seqnum.Value(tcpHdr.SequenceNumber())
					b.Release()

					// Send ACK.
					c.SendPacket(nil, &testcontext.Headers{
						SrcPort: srcPort,
						DstPort: testcontext.StackPort,
						Flags:   header.TCPFlagAck,
						SeqNum:  iss + 1,
						AckNum:  serverISS + 1,
						RcvWnd:  30000,
					})

					acceptedEPs = append(acceptedEPs, acceptProbe(t, c))
				}
				return acceptedEPs
			},
		},
		{
			name: "HandshakeProbeConnection",
			setupProbes: func(t *testing.T, c *testcontext.Context) []tcpip.Endpoint {
				// Send SYN to create a half-open handshake connection in the accept queue.
				synOpts := make([]byte, header.TCPOptionsMaximumSize)
				optOffset := header.EncodeMSSOption(uint32(e2e.DefaultIPv4MSS), synOpts)
				padding := 4 - optOffset%4
				for j := optOffset; j < optOffset+padding; j++ {
					synOpts[j] = header.TCPOptionNOP
				}
				optOffset += padding

				c.SendPacket(nil, &testcontext.Headers{
					SrcPort: testcontext.TestPort,
					DstPort: testcontext.StackPort,
					Flags:   header.TCPFlagSyn,
					SeqNum:  seqnum.Value(testcontext.TestInitialSequenceNumber),
					RcvWnd:  30000,
					TCPOpts: synOpts[:optOffset],
				})

				// Drain SYN-ACK from server.
				b := c.GetPacket()
				b.Release()

				// Do not send final ACK, leaving the connection in handshake state.
				return nil
			},
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			for _, reverse := range []bool{false, true} {
				orderName := "DefaultOrder"
				if reverse {
					orderName = "ReversedOrder"
				}
				t.Run(orderName, func(t *testing.T) {
					c := testcontext.New(t, e2e.DefaultMTU)
					defer c.Cleanup()

					c.Stack().SetAllowLiveTCPMigration(false)
					c.Stack().SetRemoveConf(true)

					c.Create(-1)
					if err := c.EP.Bind(tcpip.FullAddress{Port: testcontext.StackPort}); err != nil {
						t.Fatalf("Bind failed: %v", err)
					}
					if err := c.EP.Listen(10); err != nil {
						t.Fatalf("Listen failed: %v", err)
					}

					// Establish probe connections.
					probes := tc.setupProbes(t, c)
					for _, probe := range probes {
						defer probe.Close()
					}

					// Save the stack.
					var buf bytes.Buffer
					saveStats, err := state.Save(context.Background(), &buf, c.Stack())
					if err != nil {
						t.Fatalf("Save failed: %v", err)
					}
					t.Logf("Save stats:\n%s", saveStats.String())

					// Restore the stack in a new instance with AllowLiveTCPMigration=false.
					restoredStack := stack.New(stack.Options{
						TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol},
						NetworkProtocols:   []stack.NetworkProtocolFactory{ipv4.NewProtocol, ipv6.NewProtocol},
					})
					restoredStack.SetAllowLiveTCPMigration(false)
					defer restoredStack.Destroy()

					loadStats, err := state.Load(context.Background(), bytes.NewReader(buf.Bytes()), restoredStack)
					if err != nil {
						t.Fatalf("Load failed: %v", err)
					}
					t.Logf("Load stats:\n%s", loadStats.String())

					// Recreate NIC 1 with same IP.
					ep := channel.New(1000, e2e.DefaultMTU, "")
					if err := restoredStack.CreateNIC(1, ep); err != nil {
						t.Fatalf("CreateNIC failed: %v", err)
					}
					if err := restoredStack.AddProtocolAddress(1, tcpip.ProtocolAddress{
						Protocol:          header.IPv4ProtocolNumber,
						AddressWithPrefix: testcontext.StackAddrWithPrefix,
					}, stack.AddressProperties{}); err != nil {
						t.Fatalf("AddProtocolAddress failed: %v", err)
					}
					restoredStack.SetRouteTable([]tcpip.Route{
						{
							Destination: header.IPv4EmptySubnet,
							NIC:         1,
						},
					})

					// Inspect restored endpoints before Restore().
					stackType := reflect.TypeOf(restoredStack).Elem()
					sf, ok := stackType.FieldByName("restoredEndpoints")
					if !ok {
						t.Fatalf("Field restoredEndpoints not found")
					}
					offset := sf.Offset
					ptr := unsafe.Pointer(uintptr(unsafe.Pointer(restoredStack)) + offset)
					restoredEndpointsPtr := (*[]stack.RestoredEndpoint)(ptr)
					restoredEndpoints := *restoredEndpointsPtr

					// Mark all non-listen restored endpoints with terminateAtRestore = true to simulate snapshot restore.
					for _, rep := range restoredEndpoints {
						epVal := reflect.ValueOf(rep)
						if epVal.Kind() == reflect.Ptr {
							origStateField := epVal.Elem().FieldByName("origEndpointState")
							if origStateField.IsValid() && origStateField.Uint() != uint64(tcp.StateListen) {
								termField := epVal.Elem().FieldByName("terminateAtRestore")
								if termField.IsValid() {
									// Use unsafe to set unexported field terminateAtRestore
									tf, _ := epVal.Elem().Type().FieldByName("terminateAtRestore")
									tPtr := (*bool)(unsafe.Pointer(uintptr(epVal.UnsafePointer()) + tf.Offset))
									*tPtr = true
								}
							}
						}
					}

					if reverse {
						var reordered []stack.RestoredEndpoint
						for i := len(restoredEndpoints) - 1; i >= 0; i-- {
							reordered = append(reordered, restoredEndpoints[i])
						}
						*restoredEndpointsPtr = reordered
					}

					// Find listening endpoint before Restore() clears s.restoredEndpoints.
					var listenEP tcpip.Endpoint
					for _, rep := range *restoredEndpointsPtr {
						epVal := reflect.ValueOf(rep)
						if epVal.Kind() == reflect.Ptr {
							origStateField := epVal.Elem().FieldByName("origEndpointState")
							if origStateField.IsValid() && origStateField.Uint() == uint64(tcp.StateListen) {
								listenEP = rep.(tcpip.Endpoint)
								break
							}
						}
					}

					if listenEP == nil {
						t.Fatalf("Listening endpoint for port %d not found in restored endpoints", testcontext.StackPort)
					}

					// Call Restore with timeout to detect deadlock.
					restoreDone := make(chan struct{})
					go func() {
						restoredStack.Restore()
						close(restoreDone)
					}()

					select {
					case <-restoreDone:
					case <-time.After(5 * time.Second):
						t.Fatal("Deadlock in Restore(): hung waiting on endpoint restoration")
					}

					// Wait for asynchronous restore loading to complete.
					tcpip.AsyncLoading.Wait()

					// Verify that the listening endpoint is in StateListen and not stuck in StateInitial/StateError.
					if got, want := tcp.EndpointState(listenEP.State()), tcp.StateListen; got != want {
						t.Fatalf("Unexpected listening endpoint state: want %v, got %v", want, got)
					}

					// Try establishing a new connection to the restored listening socket.
					// Inject a SYN packet from a new client port (TestPort+100) to StackPort.
					newClientPort := uint16(testcontext.TestPort + 100)
					clientISS := seqnum.Value(testcontext.TestInitialSequenceNumber + 5000)
					synOpts := make([]byte, header.TCPOptionsMaximumSize)
					optOffset := header.EncodeMSSOption(uint32(e2e.DefaultIPv4MSS), synOpts)
					padding := 4 - optOffset%4
					for i := optOffset; i < optOffset+padding; i++ {
						synOpts[i] = header.TCPOptionNOP
					}
					optOffset += padding

					synHdr := c.BuildSegment(nil, &testcontext.Headers{
						SrcPort: newClientPort,
						DstPort: testcontext.StackPort,
						Flags:   header.TCPFlagSyn,
						SeqNum:  clientISS,
						RcvWnd:  30000,
						TCPOpts: synOpts[:optOffset],
					})
					pkt := stack.NewPacketBuffer(stack.PacketBufferOptions{
						Payload: synHdr,
					})
					ep.InjectInbound(ipv4.ProtocolNumber, pkt)
					pkt.DecRef()

					// Read response from restoredStack.
					readCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
					defer cancel()
					respPkt := ep.ReadContext(readCtx)
					if respPkt == nil {
						t.Fatalf("Restored listening socket failed to respond to SYN")
					}
					defer respPkt.DecRef()

					respView := respPkt.ToView()
					defer respView.Release()
					checker.IPv4(t, respView, checker.TCP(
						checker.SrcPort(testcontext.StackPort),
						checker.DstPort(newClientPort),
						checker.TCPFlags(header.TCPFlagAck|header.TCPFlagSyn),
						checker.TCPAckNum(uint32(clientISS)+1),
					))
				})
			}
		})
	}
}

type unregisteredClock struct {
	tcpip.Clock
}

func TestSaveAfterRestoreWithReplaceConfig(t *testing.T) {
	c := testcontext.New(t, e2e.DefaultMTU)
	defer c.Cleanup()

	const preservedNICID = 10
	preservedIPv4Addr := tcpip.AddressWithPrefix{
		Address:   tcpip.AddrFrom4([4]byte{10, 0, 1, 1}),
		PrefixLen: 24,
	}
	preservedIPv6Addr := tcpip.AddressWithPrefix{
		Address:   tcpip.AddrFrom16([16]byte{0xfd, 0x00, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1}),
		PrefixLen: 64,
	}
	preservedEP := channel.New(1000, e2e.DefaultMTU, "")
	defer preservedEP.Close()
	if err := c.Stack().CreateNICWithOptions(preservedNICID, preservedEP, stack.NICOptions{Kind: "tun"}); err != nil {
		t.Fatalf("CreateNICWithOptions failed: %v", err)
	}
	if err := c.Stack().AddProtocolAddress(preservedNICID, tcpip.ProtocolAddress{
		Protocol:          header.IPv4ProtocolNumber,
		AddressWithPrefix: preservedIPv4Addr,
	}, stack.AddressProperties{}); err != nil {
		t.Fatalf("AddProtocolAddress(IPv4) failed: %v", err)
	}
	if err := c.Stack().AddProtocolAddress(preservedNICID, tcpip.ProtocolAddress{
		Protocol:          header.IPv6ProtocolNumber,
		AddressWithPrefix: preservedIPv6Addr,
	}, stack.AddressProperties{}); err != nil {
		t.Fatalf("AddProtocolAddress(IPv6) failed: %v", err)
	}

	c.Stack().SetRemoveConf(false)

	var buf bytes.Buffer
	if _, err := state.Save(context.Background(), &buf, c.Stack()); err != nil {
		t.Fatalf("First Save failed: %v", err)
	}
	c.Stack().Resume()

	restoredStack := stack.New(stack.Options{
		TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol},
		NetworkProtocols:   []stack.NetworkProtocolFactory{arp.NewProtocol, ipv4.NewProtocol, ipv6.NewProtocol},
	})
	defer restoredStack.Destroy()

	if _, err := state.Load(context.Background(), bytes.NewReader(buf.Bytes()), restoredStack); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	// Simulate sentry restore creating unpreserved NICs on a temporary stack
	// with a non-state-registered clock before calling ReplaceConfig.
	tempStack := stack.New(stack.Options{
		TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol},
		NetworkProtocols:   []stack.NetworkProtocolFactory{arp.NewProtocol, ipv4.NewProtocol, ipv6.NewProtocol},
		Clock:              &unregisteredClock{Clock: tcpip.NewStdClock()},
	})
	defer tempStack.Destroy()

	ep := channel.New(1000, e2e.DefaultMTU, "")
	defer ep.Close()
	if err := tempStack.CreateNIC(1, ep); err != nil {
		t.Fatalf("tempStack.CreateNIC failed: %v", err)
	}
	if err := tempStack.AddProtocolAddress(1, tcpip.ProtocolAddress{
		Protocol:          header.IPv4ProtocolNumber,
		AddressWithPrefix: testcontext.StackAddrWithPrefix,
	}, stack.AddressProperties{}); err != nil {
		t.Fatalf("tempStack.AddProtocolAddress(IPv4) failed: %v", err)
	}
	if err := tempStack.AddProtocolAddress(1, tcpip.ProtocolAddress{
		Protocol:          header.IPv6ProtocolNumber,
		AddressWithPrefix: testcontext.StackV6AddrWithPrefix,
	}, stack.AddressProperties{}); err != nil {
		t.Fatalf("tempStack.AddProtocolAddress(IPv6) failed: %v", err)
	}

	restoredStack.ReplaceConfig(tempStack)
	restoredStack.Restore()

	if got := restoredStack.CheckLocalAddress(preservedNICID, header.IPv4ProtocolNumber, preservedIPv4Addr.Address); got != preservedNICID {
		t.Fatalf("restoredStack.CheckLocalAddress(IPv4) = %d, want %d", got, preservedNICID)
	}
	if got := restoredStack.CheckLocalAddress(preservedNICID, header.IPv6ProtocolNumber, preservedIPv6Addr.Address); got != preservedNICID {
		t.Fatalf("restoredStack.CheckLocalAddress(IPv6) = %d, want %d", got, preservedNICID)
	}

	// Taking a subsequent snapshot with removeConf=false must not serialize
	// unpreserved NICs from tempStack or fail on unregisteredClock.
	restoredStack.SetRemoveConf(false)
	var buf2 bytes.Buffer
	if _, err := state.Save(context.Background(), &buf2, restoredStack); err != nil {
		t.Fatalf("Second Save after Restore+ReplaceConfig failed: %v", err)
	}
	restoredStack.Resume()

	restoredStack2 := stack.New(stack.Options{
		TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol},
		NetworkProtocols:   []stack.NetworkProtocolFactory{arp.NewProtocol, ipv4.NewProtocol, ipv6.NewProtocol},
	})
	defer restoredStack2.Destroy()

	if _, err := state.Load(context.Background(), bytes.NewReader(buf2.Bytes()), restoredStack2); err != nil {
		t.Fatalf("Second Load failed: %v", err)
	}
	restoredStack2.Restore()

	if got := restoredStack2.CheckLocalAddress(preservedNICID, header.IPv4ProtocolNumber, preservedIPv4Addr.Address); got != preservedNICID {
		t.Fatalf("restoredStack2.CheckLocalAddress(IPv4) = %d, want %d", got, preservedNICID)
	}
	if got := restoredStack2.CheckLocalAddress(preservedNICID, header.IPv6ProtocolNumber, preservedIPv6Addr.Address); got != preservedNICID {
		t.Fatalf("restoredStack2.CheckLocalAddress(IPv6) = %d, want %d", got, preservedNICID)
	}
}
