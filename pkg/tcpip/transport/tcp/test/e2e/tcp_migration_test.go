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
			c := testcontext.New(t, e2e.DefaultMTU)
			defer c.Cleanup()

			c.Stack().SetAllowLiveTCPMigration(tc.allowMigration)
			c.Stack().SetRemoveConf(tc.removeConf)

			// Establish connection.
			c.CreateConnected(testcontext.TestInitialSequenceNumber, 30000, -1 /* epRcvBuf */)

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

			if tc.hasRouteAfterRestore {
				// Recreate the NIC 1 with same IP.
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

func TestRenegingHeadAcrossLiveRestore(t *testing.T) {
	for _, scenario := range []string{"unsaved", "restored"} {
		t.Run(scenario, func(t *testing.T) {
			const mss, offered, size = 1000, 1036, 5000
			original := faketime.NewManualClock()
			type observation struct {
				una seqnum.Value
				rto time.Duration
				mss int
			}
			samples := make(chan observation, 64)
			probe := func(s *tcp.TCPEndpointState) {
				samples <- observation{s.Sender.SndUna, s.Sender.RTO, s.Sender.MaxPayloadSize}
			}
			c := testcontext.NewWithOpts(t, testcontext.Options{MTU: 1500, EnableV4: true, Clock: original, Probe: probe})
			defer c.Cleanup()
			c.Stack().SetAllowLiveTCPMigration(true)
			c.Stack().SetRemoveConf(true)
			c.SetGSOEnabled(true)
			e2e.SetStackSACKPermitted(t, c, true)
			e2e.SetStackTCPRecovery(t, c, 0)
			c.CreateConnectedWithRawOptions(testcontext.TestInitialSequenceNumber, 30000, -1, []byte{header.TCPOptionMSS, 4, offered >> 8, offered & 255, header.TCPOptionSACKPermitted, header.TCPOptionSackPermittedLength, header.TCPOptionNOP, header.TCPOptionNOP})
			data := make([]byte, size)
			for i := range data {
				data[i] = byte(i*37 + i/251)
			}
			if n, err := c.EP.Write(bytes.NewReader(data), tcpip.WriteOptions{}); n != size || err != nil {
				t.Fatalf("write=(%d,%v)", n, err)
			}
			p := c.GetPacket()
			h := header.TCP(header.IPv4(p.AsSlice()).Payload())
			base := seqnum.Value(h.SequenceNumber())
			if !bytes.Equal(h.Payload(), data) {
				t.Fatalf("original write length=%d want one %d-byte aggregate", len(h.Payload()), size)
			}
			p.Release()
			peer := seqnum.Value(testcontext.TestInitialSequenceNumber).Add(1)
			blocks := func(ranges ...[2]int) []header.SACKBlock {
				var bs []header.SACKBlock
				for _, r := range ranges {
					bs = append(bs, header.SACKBlock{Start: base.Add(seqnum.Size(r[0])), End: base.Add(seqnum.Size(r[1]))})
				}
				return bs
			}
			expect := func(off int) {
				t.Helper()
				select {
				case s := <-samples:
					if s.una != base.Add(seqnum.Size(off)) || s.mss != mss || s.rto <= 40*time.Millisecond {
						t.Fatalf("ACK %d: got UNA=%d relative, MSS=%d RTO=%v", off, base.Size(s.una), s.mss, s.rto)
					}
				case <-time.After(5 * time.Second):
					t.Fatalf("ACK %d not processed", off)
				}
			}
			ack := func(off int, ranges ...[2]int) {
				t.Helper()
				c.SendAckWithSACK(peer, off, blocks(ranges...))
				expect(off)
			}
			ack(0, [2]int{1000, 2000})
			ack(1500)
			// Examine packets, not timer fields. Advancing in millisecond steps also
			// distinguishes an immediate fast retransmit from the short timer.
			type sample struct{ tick, off, n int }
			now := original
			read := func() []byte {
				v := c.GetPacketNonBlocking()
				if v == nil {
					return nil
				}
				defer v.Release()
				return bytes.Clone(v.AsSlice())
			}
			window := func(stage string, wantOff int) bool {
				t.Helper()
				var seen []sample
				for tick := 0; tick <= 40; tick++ {
					if tick != 0 {
						now.Advance(time.Millisecond)
					}
					for {
						b := read()
						if b == nil {
							break
						}
						h := header.TCP(header.IPv4(b).Payload())
						off := int(base.Size(seqnum.Value(h.SequenceNumber())))
						n := len(h.Payload())
						if n == 0 || off != wantOff || off+n > size || !bytes.Equal(h.Payload(), data[off:off+n]) {
							t.Fatalf("%s tick=%d unexpected segment offset=%d bytes=%d, want original bytes starting %d", stage, tick, off, n, wantOff)
						}
						seen = append(seen, sample{tick, off, n})
					}
				}
				if len(seen) > 1 {
					t.Fatalf("%s multiple retransmissions %v", stage, seen)
				}
				s := sample{-1, -1, 0}
				if len(seen) != 0 {
					s = seen[0]
					if s.tick == 0 {
						t.Fatalf("%s sent immediately", stage)
					}
				}
				t.Logf("RESTORE_WIRE stage=%s tick=%d offset=%d bytes=%d", stage, s.tick, s.off, s.n)
				return len(seen) > 0
			}
			if scenario == "unsaved" {
				window("unsaved-whole", 1500)
				ack(size)
				return
			}
			var image bytes.Buffer
			if _, err := state.Save(context.Background(), &image, c.Stack()); err != nil {
				t.Fatalf("save: %v", err)
			}
			after := stack.New(stack.Options{Clock: faketime.NewManualClock(), TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol}, NetworkProtocols: []stack.NetworkProtocolFactory{ipv4.NewProtocol, ipv6.NewProtocol}})
			after.SetAllowLiveTCPMigration(true)
			defer after.Destroy()
			if _, err := state.Load(context.Background(), bytes.NewReader(image.Bytes()), after); err != nil {
				t.Fatalf("load: %v", err)
			}
			link := channel.New(1000, 1500, "")
			if err := after.CreateNIC(1, link); err != nil {
				t.Fatal(err)
			}
			if err := after.AddProtocolAddress(1, tcpip.ProtocolAddress{Protocol: header.IPv4ProtocolNumber, AddressWithPrefix: testcontext.StackAddrWithPrefix}, stack.AddressProperties{}); err != nil {
				t.Fatal(err)
			}
			after.SetRouteTable([]tcpip.Route{{Destination: header.IPv4EmptySubnet, NIC: 1}})
			after.Restore()
			tcpip.AsyncLoading.Wait()
			var restored *tcp.Endpoint
			for _, ep := range after.RegisteredEndpoints() {
				if e, ok := ep.(*tcp.Endpoint); ok {
					if restored != nil && restored != e {
						t.Fatal("multiple restored sockets")
					}
					restored = e
				}
			}
			if restored == nil || tcp.EndpointState(restored.State()) != tcp.StateEstablished {
				t.Fatal("no restored established connection")
			}
			restored.SetQualificationSACKProbe(probe)
			now = after.Clock().(*faketime.ManualClock)
			read = func() []byte {
				p := link.Read()
				if p == nil {
					return nil
				}
				defer p.DecRef()
				v := p.ToView()
				defer v.Release()
				return bytes.Clone(v.AsSlice())
			}
			inject := func(off int, ranges ...[2]int) {
				t.Helper()
				var opts [40]byte
				n := 0
				if len(ranges) != 0 {
					n += header.EncodeNOP(opts[n:])
					n += header.EncodeNOP(opts[n:])
					n += header.EncodeSACKBlocks(blocks(ranges...), opts[n:])
				}
				p := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: c.BuildSegment(nil, &testcontext.Headers{SrcPort: testcontext.TestPort, DstPort: c.Port, Flags: header.TCPFlagAck, SeqNum: peer, AckNum: base.Add(seqnum.Size(off)), RcvWnd: 30000, TCPOpts: opts[:n]})})
				defer p.DecRef()
				link.InjectInbound(ipv4.ProtocolNumber, p)
				expect(off)
			}
			// Leave one unreported byte: a range beginning exactly at the cumulative
			// ACK is not a valid SACK block.
			inject(1501, [2]int{1502, 2000}, [2]int{4000, 5000})
			if !window("restored-suffix", 1501) {
				// This is a new report for the next transmitted packet, not a repeat
				// of the SACK used before the save. Stop halfway through that packet.
				inject(1501, [2]int{2000, 3000})
				inject(2500)
				window("restored-fresh", 2500)
			}
			inject(size)
		})
	}
}
