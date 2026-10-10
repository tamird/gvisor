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

	"github.com/google/go-cmp/cmp"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/checker"
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
	testCases := []struct {
		name        string
		setupProbes func(t *testing.T, c *testcontext.Context) []tcpip.Endpoint
	}{
		{
			name: "EstablishedProbeConnection",
			setupProbes: func(t *testing.T, c *testcontext.Context) []tcpip.Endpoint {
				waitEntry, ch := waiter.NewChannelEntry(waiter.ReadableEvents)
				c.WQ.EventRegister(&waitEntry)
				defer c.WQ.EventUnregister(&waitEntry)

				c.PassiveConnect(100, -1, header.TCPSynOptions{MSS: e2e.DefaultIPv4MSS})

				acceptedEP, _, tcpErr := c.EP.Accept(nil)
				if cmp.Equal(&tcpip.ErrWouldBlock{}, tcpErr) {
					select {
					case <-ch:
						acceptedEP, _, tcpErr = c.EP.Accept(nil)
						if tcpErr != nil {
							t.Fatalf("Accept failed: %v", tcpErr)
						}
					case <-time.After(5 * time.Second):
						t.Fatalf("Timed out waiting for accept")
					}
				} else if tcpErr != nil {
					t.Fatalf("Accept failed: %v", tcpErr)
				}
				return []tcpip.Endpoint{acceptedEP}
			},
		},
		{
			name: "CloseWaitProbeConnection",
			setupProbes: func(t *testing.T, c *testcontext.Context) []tcpip.Endpoint {
				waitEntry, ch := waiter.NewChannelEntry(waiter.ReadableEvents)
				c.WQ.EventRegister(&waitEntry)
				defer c.WQ.EventUnregister(&waitEntry)

				c.PassiveConnect(100, -1, header.TCPSynOptions{MSS: e2e.DefaultIPv4MSS})

				acceptedEP, _, tcpErr := c.EP.Accept(nil)
				if cmp.Equal(&tcpip.ErrWouldBlock{}, tcpErr) {
					select {
					case <-ch:
						acceptedEP, _, tcpErr = c.EP.Accept(nil)
						if tcpErr != nil {
							t.Fatalf("Accept failed: %v", tcpErr)
						}
					case <-time.After(5 * time.Second):
						t.Fatalf("Timed out waiting for accept")
					}
				} else if tcpErr != nil {
					t.Fatalf("Accept failed: %v", tcpErr)
				}

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
				waitEntry, ch := waiter.NewChannelEntry(waiter.ReadableEvents)
				c.WQ.EventRegister(&waitEntry)
				defer c.WQ.EventUnregister(&waitEntry)

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

					acceptedEP, _, tcpErr := c.EP.Accept(nil)
					if cmp.Equal(&tcpip.ErrWouldBlock{}, tcpErr) {
						select {
						case <-ch:
							acceptedEP, _, tcpErr = c.EP.Accept(nil)
							if tcpErr != nil {
								t.Fatalf("Accept probe %d failed: %v", i, tcpErr)
							}
						case <-time.After(5 * time.Second):
							t.Fatalf("Timed out waiting for accept on probe %d", i)
						}
					} else if tcpErr != nil {
						t.Fatalf("Accept probe %d failed: %v", i, tcpErr)
					}
					acceptedEPs = append(acceptedEPs, acceptedEP)
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

// TestSACKCreditsAfterRestore is a qualification witness using the separately
// qualified StateSnapshot API. It is not part of the standalone SACK patch.
func TestSACKCreditsAfterRestore(t *testing.T) {
	const payload = 32
	const mtu = header.IPv4MinimumSize + header.TCPMinimumSize + header.TCPOptionsMaximumSize + payload
	c := testcontext.New(t, mtu)
	defer c.Cleanup()
	c.Stack().SetAllowLiveTCPMigration(true)
	c.Stack().SetRemoveConf(true)
	e2e.SetStackSACKPermitted(t, c, true)
	e2e.SetStackTCPRecovery(t, c, 0)
	// Keep ordinary retransmission outside the checkpoint operation; no
	// custom clock or callback is serialized with the stack.
	minRTO := tcpip.TCPMinRTOOption(time.Minute)
	if err := c.Stack().SetTransportProtocolOption(tcp.ProtocolNumber, &minRTO); err != nil {
		t.Fatalf("SetTransportProtocolOption(TCPMinRTOOption): %s", err)
	}
	e2e.CreateConnectedWithSACKAndTS(c)
	data := make([]byte, 5*payload)
	n, err := c.EP.Write(bytes.NewReader(data), tcpip.WriteOptions{})
	if err != nil {
		t.Fatalf("Write: %s", err)
	}
	if got, want := n, int64(len(data)); got != want {
		t.Fatalf("Write = %d, want %d", got, want)
	}
	for offset := 0; offset < len(data); offset += payload {
		c.ReceiveAndCheckPacketWithOptions(data, offset, payload, e2e.TSOptionSize)
	}
	seq := seqnum.Value(testcontext.TestInitialSequenceNumber).Add(1)
	c.SendAck(seq, payload)
	c.Stack().Pause()
	c.Stack().Resume()
	start := c.IRS.Add(1 + 2*payload)
	c.SendAckWithSACK(seq, payload, []header.SACKBlock{{Start: start, End: start.Add(2 * payload)}})
	c.Stack().Pause()
	c.Stack().Resume()
	before, err := c.EP.(*tcp.Endpoint).StateSnapshot()
	if err != nil {
		t.Fatalf("StateSnapshot before Save: %s", err)
	}
	if got, want := before.Sender.SackedOut, 2; got != want {
		t.Fatalf("SackedOut before Save = %d, want %d", got, want)
	}
	if got, want := before.Sender.SACKedBytes, seqnum.Size(2*payload); got != want {
		t.Fatalf("SACKed bytes before Save = %d, want %d", got, want)
	}
	if got, want := before.Sender.UnacknowledgedSequenceBytes, seqnum.Size(4*payload); got != want {
		t.Fatalf("unacknowledged bytes before Save = %d, want %d", got, want)
	}
	if got, want := before.SndBufState.SndBufUsed, 4*payload; got != want {
		t.Fatalf("queued bytes before Save = %d, want %d", got, want)
	}
	var saved bytes.Buffer
	if _, err := state.Save(context.Background(), &saved, c.Stack()); err != nil {
		t.Fatalf("Save: %s", err)
	}
	restored := stack.New(stack.Options{
		TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol},
		NetworkProtocols:   []stack.NetworkProtocolFactory{ipv4.NewProtocol, ipv6.NewProtocol},
	})
	defer restored.Destroy()
	if _, err := state.Load(context.Background(), bytes.NewReader(saved.Bytes()), restored); err != nil {
		t.Fatalf("Load: %s", err)
	}
	link := channel.New(1000, mtu, "")
	if err := restored.CreateNIC(1, link); err != nil {
		t.Fatalf("CreateNIC: %s", err)
	}
	if err := restored.AddProtocolAddress(1, tcpip.ProtocolAddress{
		Protocol:          header.IPv4ProtocolNumber,
		AddressWithPrefix: testcontext.StackAddrWithPrefix,
	}, stack.AddressProperties{}); err != nil {
		t.Fatalf("AddProtocolAddress: %s", err)
	}
	restored.SetRouteTable([]tcpip.Route{{Destination: header.IPv4EmptySubnet, NIC: 1}})
	restored.Restore()
	endpoints := restored.RegisteredEndpoints()
	if got, want := len(endpoints), 1; got != want {
		t.Fatalf("restored endpoints = %d, want %d", got, want)
	}
	ep, ok := endpoints[0].(*tcp.Endpoint)
	if !ok {
		t.Fatalf("restored endpoint = %T, want *tcp.Endpoint", endpoints[0])
	}
	after, err := ep.StateSnapshot()
	if err != nil {
		t.Fatalf("StateSnapshot after Restore: %s", err)
	}
	if got, want := after.ID, before.ID; got != want {
		t.Errorf("restored connection identity = %+v, want %+v", got, want)
	}
	if got, want := after.Sender.SndUna, before.Sender.SndUna; got != want {
		t.Errorf("restored SndUna = %d, want %d", got, want)
	}
	if got, want := after.Sender.SndNxt, before.Sender.SndNxt; got != want {
		t.Errorf("restored SndNxt = %d, want %d", got, want)
	}
	if got, want := after.Sender.SndCwnd, before.Sender.SndCwnd; got != want {
		t.Errorf("restored cwnd = %d, want %d (no RTO or new sender)", got, want)
	}
	if got, want := after.SndBufState.SndBufUsed, before.SndBufState.SndBufUsed; got != want {
		t.Errorf("restored queued bytes = %d, want %d", got, want)
	}
	if got, want := len(after.SACK.ReceivedBlocks), 0; got != want {
		t.Errorf("restored scoreboard ranges = %d, want %d", got, want)
	}
	if got, want := after.Sender.SACKedBytes, seqnum.Size(0); got != want {
		t.Errorf("restored SACKed bytes = %d, want %d", got, want)
	}
	if got, want := after.Sender.UnacknowledgedSequenceBytes, before.Sender.UnacknowledgedSequenceBytes; got != want {
		t.Errorf("restored unacknowledged bytes = %d, want %d", got, want)
	}
	if got, want := after.Sender.SackedOut, 0; got != want {
		t.Errorf("restored SackedOut = %d, want %d", got, want)
	}
}
