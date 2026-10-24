// Copyright 2021 The gVisor Authors.
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

package tcp_syncookie_test

import (
	"flag"
	"fmt"
	"math"
	"testing"
	"time"

	"github.com/google/go-cmp/cmp"
	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/test/packetimpact/testbench"
)

func init() {
	testbench.Initialize(flag.CommandLine)
}

func TestSynCookieSACK(t *testing.T) {
	dut := testbench.NewDUT(t)
	listenFD, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
	t.Cleanup(func() { dut.Close(t, listenFD) })

	sendSYN := func(timestamps bool) (*testbench.TCPIPv4, header.TCPSynOptions) {
		t.Helper()
		conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &port}, testbench.TCP{SrcPort: &port})
		t.Cleanup(func() { conn.Close(t) })
		var options [header.TCPOptionsMaximumSize]byte
		n := header.EncodeSACKPermittedOption(options[:])
		if timestamps {
			n += header.EncodeTSOption(1, 0, options[n:])
		}
		n += header.AddTCPOptionPadding(options[:], n)
		conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn), Options: options[:n]})
		reply, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn | header.TCPFlagAck)}, time.Second)
		if err != nil {
			t.Fatalf("waiting for SYN-ACK: %v", err)
		}
		return &conn, header.ParseSynOptions(reply.Options, true /* isAck */)
	}

	// Leave enough handshakes unfinished to fill the SYN queue. A cookie
	// cannot preserve the SACK option when the peer omits timestamps.
	if _, opts := sendSYN(false); !opts.SACKPermitted {
		t.Fatal("ordinary SYN-ACK did not permit SACK")
	}
	const maxHalfOpen = 16
	for i := range maxHalfOpen {
		if _, opts := sendSYN(false); !opts.SACKPermitted {
			break
		}
		if i == maxHalfOpen-1 {
			t.Fatal("listener still negotiated SACK without timestamps on a full SYN queue")
		}
	}
	conn, opts := sendSYN(true)
	if !opts.TS || opts.TSEcr != 1 || !opts.SACKPermitted {
		t.Fatalf("cookie SYN-ACK options = %+v, want echoed TSVal 1 and SACK", opts)
	}

	var ts [12]byte
	n := header.EncodeTSOption(2, opts.TSVal, ts[:])
	header.AddTCPOptionPadding(ts[:], n)
	ack := testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), Options: ts[:]}
	conn.Send(t, ack)
	dut.PollOne(t, listenFD, unix.POLLIN, time.Second)
	fd, _ := dut.Accept(t, listenFD)
	t.Cleanup(func() { dut.Close(t, fd) })

	// A SACK block for the byte beyond the hole proves that the accepted
	// socket recovered the option from the handshake.
	hole := *conn.LocalSeqNum(t)
	ack.SeqNum = testbench.Uint32(uint32(hole + 1))
	conn.Send(t, ack, &testbench.Payload{Bytes: []byte{1}})
	reply, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(hole))}, time.Second)
	if err != nil {
		t.Fatalf("waiting for ACK of out-of-order data: %v", err)
	}
	got := header.ParseTCPOptions(reply.Options).SACKBlocks
	want := []header.SACKBlock{{Start: hole + 1, End: hole + 2}}
	if diff := cmp.Diff(want, got); diff != "" {
		t.Errorf("SACK blocks (-want +got):\n%s", diff)
	}
}

// TestSynCookieWindowScale checks both the negotiated option and the send
// window restored from a cookie handshake.
func TestSynCookieWindowScale(t *testing.T) {
	for _, timestamps := range []bool{false, true} {
		t.Run(fmt.Sprintf("timestamps=%t", timestamps), func(t *testing.T) {
			dut := testbench.NewDUT(t)
			listenFD, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
			t.Cleanup(func() { dut.Close(t, listenFD) })

			const peerShift = 3
			const windowField = 16
			sendSYN := func(ts bool) (*testbench.TCPIPv4, header.TCPSynOptions) {
				t.Helper()
				conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &port, WindowSize: testbench.Uint16(windowField)}, testbench.TCP{SrcPort: &port})
				t.Cleanup(func() { conn.Close(t) })
				var options [header.TCPOptionsMaximumSize]byte
				n := header.EncodeSACKPermittedOption(options[:])
				n += header.EncodeWSOption(peerShift, options[n:])
				if ts {
					n += header.EncodeTSOption(1, 0, options[n:])
				}
				n += header.AddTCPOptionPadding(options[:], n)
				conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn), Options: options[:n]})
				reply, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn | header.TCPFlagAck)}, time.Second)
				if err != nil {
					t.Fatalf("waiting for SYN-ACK: %v", err)
				}
				return &conn, header.ParseSynOptions(reply.Options, true /* isAck */)
			}

			// Detect the actual cookie boundary rather than assuming a
			// particular kernel's interpretation of the listen backlog.
			if _, opts := sendSYN(false); !opts.SACKPermitted || opts.WS < 0 {
				t.Fatalf("ordinary SYN-ACK options = %+v, want SACK and window scaling", opts)
			}
			const maxHalfOpen = 16
			for i := range maxHalfOpen {
				if _, opts := sendSYN(false); !opts.SACKPermitted {
					break
				}
				if i == maxHalfOpen-1 {
					t.Fatal("listener still negotiated SACK without timestamps on a full SYN queue")
				}
			}
			conn, opts := sendSYN(timestamps)
			wantWindow := windowField
			ack := testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), WindowSize: testbench.Uint16(windowField)}
			if timestamps {
				if !opts.TS || opts.TSEcr != 1 {
					t.Fatalf("cookie SYN-ACK options = %+v, want echoed TSVal 1", opts)
				}
				if opts.WS < 0 {
					t.Errorf("timestamped cookie SYN-ACK omitted window scaling: %+v", opts)
				}
				var ts [12]byte
				n := header.EncodeTSOption(2, opts.TSVal, ts[:])
				header.AddTCPOptionPadding(ts[:], n)
				ack.Options = ts[:]
				wantWindow <<= peerShift
			} else if opts.WS != -1 || opts.TS || opts.SACKPermitted {
				t.Fatalf("cookie without timestamps retained unsupported options: %+v", opts)
			}
			conn.Send(t, ack)
			dut.PollOne(t, listenFD, unix.POLLIN, time.Second)
			fd, _ := dut.Accept(t, listenFD)
			t.Cleanup(func() { dut.Close(t, fd) })
			dut.SetSockOptInt(t, fd, unix.IPPROTO_TCP, unix.TCP_NODELAY, 1)

			// One byte beyond the advertised scaled window must remain
			// queued until our cumulative ACK opens the window again.
			data := make([]byte, wantWindow+1)
			for i := range data {
				data[i] = byte(i)
			}
			if got := dut.Send(t, fd, data, 0); got != int32(len(data)) {
				t.Fatalf("send accepted %d bytes, want %d", got, len(data))
			}
			frame, err := conn.ExpectData(t, &testbench.TCP{}, nil, time.Second)
			if err != nil {
				t.Fatalf("waiting for initial window data: %v", err)
			}
			payload, ok := frame[len(frame)-1].(*testbench.Payload)
			if !ok {
				t.Fatalf("initial window response has no payload: %s", frame)
			}
			t.Logf("COOKIE_WS timestamps=%t peer_shift=%d synack_shift=%d window_field=%d initial_bytes=%d want_bytes=%d", timestamps, peerShift, opts.WS, windowField, len(payload.Bytes), wantWindow)
			if diff := cmp.Diff(data[:wantWindow], payload.Bytes); diff != "" {
				t.Fatalf("initial window data (-want +got):\n%s", diff)
			}
			tail := &testbench.Payload{Bytes: data[wantWindow:]}
			if _, err := conn.ExpectData(t, &testbench.TCP{}, tail, 100*time.Millisecond); err == nil {
				t.Fatal("data beyond the advertised window arrived before an ACK")
			}
			conn.Send(t, ack)
			if _, err := conn.ExpectNextData(t, &testbench.TCP{}, tail, time.Second); err != nil {
				t.Fatalf("queued byte did not arrive after window reopened: %v", err)
			}
			conn.Send(t, ack)
		})
	}
}

// TestTCPSynCookie tests for ACK handling for connections in SYNRCVD state
// connections with and without syncookies. It verifies if the passive open
// connection is indeed using syncookies before proceeding.
func TestTCPSynCookie(t *testing.T) {
	dut := testbench.NewDUT(t)
	for _, tt := range []struct {
		accept bool
		flags  header.TCPFlags
	}{
		{accept: true, flags: header.TCPFlagAck},
		{accept: true, flags: header.TCPFlagAck | header.TCPFlagPsh},
		{accept: false, flags: header.TCPFlagAck | header.TCPFlagSyn},
		{accept: true, flags: header.TCPFlagAck | header.TCPFlagFin},
		{accept: false, flags: header.TCPFlagAck | header.TCPFlagRst},
		{accept: false, flags: header.TCPFlagRst},
	} {
		t.Run(fmt.Sprintf("flags=%s", tt.flags), func(t *testing.T) {
			// Make a copy before parallelizing the test and refer to that
			// within the test. Otherwise, the test reference could be pointing
			// to an incorrect variant based on how it is scheduled.
			test := tt

			t.Parallel()

			// Listening endpoint accepts one more connection than the listen
			// backlog. Listener starts using syncookies when it sees a new SYN
			// and has backlog size of connections in SYNRCVD state. Keep the
			// listen backlog 1, so that the test can define 2 connections
			// without and with using syncookies.
			listenFD, remotePort := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1 /*backlog*/)
			defer dut.Close(t, listenFD)

			var withoutSynCookieConn testbench.TCPIPv4
			var withSynCookieConn testbench.TCPIPv4

			for _, conn := range []*testbench.TCPIPv4{&withoutSynCookieConn, &withSynCookieConn} {
				*conn = dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &remotePort}, testbench.TCP{SrcPort: &remotePort})
			}
			defer withoutSynCookieConn.Close(t)
			defer withSynCookieConn.Close(t)

			// Setup the 2 connections in SYNRCVD state and verify if one of the
			// connection is indeed using syncookies by checking for absence of
			// SYNACK retransmits.
			for _, c := range []struct {
				desc             string
				conn             *testbench.TCPIPv4
				expectRetransmit bool
			}{
				{desc: "without syncookies", conn: &withoutSynCookieConn, expectRetransmit: true},
				{desc: "with syncookies", conn: &withSynCookieConn, expectRetransmit: false},
			} {
				t.Run(c.desc, func(t *testing.T) {
					// Expect dut connection to have transitioned to SYNRCVD state.
					c.conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn)})
					if _, err := c.conn.ExpectData(t, &testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn | header.TCPFlagAck)}, nil, time.Second); err != nil {
						t.Fatalf("expected SYNACK, but got %s", err)
					}

					// If the DUT listener is using syn cookies, it will not retransmit SYNACK.
					got, err := c.conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(*c.conn.RemoteSeqNum(t) - 1)), Flags: testbench.TCPFlags(header.TCPFlagSyn | header.TCPFlagAck)}, nil, 2*time.Second)
					if c.expectRetransmit && err != nil {
						t.Fatalf("expected retransmitted SYNACK, but got %s", err)
					}
					if !c.expectRetransmit && err == nil {
						// FIXME(b/421034671): it always fails on Linux.
						if !dut.Uname.IsLinux() {
							t.Fatalf("expected no retransmitted SYNACK, but got %s", got)
						}
					}
				})
			}

			// Check whether ACKs with the given flags completes the handshake.
			for _, c := range []struct {
				desc string
				conn *testbench.TCPIPv4
			}{
				{desc: "with syncookies", conn: &withSynCookieConn},
				{desc: "without syncookies", conn: &withoutSynCookieConn},
			} {
				t.Run(c.desc, func(t *testing.T) {
					pfds := dut.Poll(t, []unix.PollFd{{Fd: listenFD, Events: math.MaxInt16}}, 0 /*timeout*/)
					if got, want := len(pfds), 0; got != want {
						t.Fatalf("dut.Poll(...) = %d, want = %d", got, want)
					}

					sampleData := []byte("Sample Data")
					samplePayload := &testbench.Payload{Bytes: sampleData}

					c.conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(test.flags)}, samplePayload)
					pfds = dut.Poll(t, []unix.PollFd{{Fd: listenFD, Events: unix.POLLIN}}, time.Second)
					want := 0
					if test.accept {
						want = 1
					}
					if got := len(pfds); got != want {
						t.Fatalf("got dut.Poll(...) = %d, want = %d", got, want)
					}
					// Accept the connection to enable poll on any subsequent connection.
					if test.accept {
						fd, _ := dut.Accept(t, listenFD)
						if test.flags.Contains(header.TCPFlagFin) {
							dut.PollOne(t, fd, unix.POLLIN|unix.POLLRDHUP, time.Second)
						}
						got := dut.Recv(t, fd, int32(len(sampleData)), 0)
						if diff := cmp.Diff(got, sampleData); diff != "" {
							t.Fatalf("dut.Recv: data mismatch (-want +got):\n%s", diff)
						}
						dut.Close(t, fd)
					}
				})
			}
		})
	}
}
