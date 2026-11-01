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

// TestSynCookieSACK checks what an accepted socket reports after data arrives
// beyond a hole, including when the peer omitted one of the required options.
func TestSynCookieSACK(t *testing.T) {
	for _, tc := range []struct {
		name             string
		sack, timestamps bool
	}{
		{name: "sack_and_timestamps", sack: true, timestamps: true},
		{name: "no_timestamps", sack: true},
		{name: "no_peer_sack", timestamps: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dut := testbench.NewDUT(t)
			listenFD, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1 /* backlog */)
			t.Cleanup(func() { dut.Close(t, listenFD) })
			toListener := testbench.TCP{DstPort: &port}
			var options [header.TCPOptionsMaximumSize]byte
			n := header.EncodeSACKPermittedOption(options[:])
			fillSYNQueue(t, &dut, toListener, options[:n+header.AddTCPOptionPadding(options[:], n)])
			if !tc.sack {
				n = 0
			}
			if tc.timestamps {
				n += header.EncodeTSOption(1, 0, options[n:])
			}
			n += header.AddTCPOptionPadding(options[:], n)
			conn, reply := sendSYN(t, &dut, toListener, options[:n])
			wantSACK := tc.sack && tc.timestamps
			if reply.SACKPermitted != wantSACK || reply.TS != tc.timestamps || (tc.timestamps && reply.TSEcr != 1) {
				t.Fatalf("cookie SYN-ACK options = %+v, want SACK %t, timestamps %t", reply, wantSACK, tc.timestamps)
			}
			ack := testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck)}
			if tc.timestamps {
				n := header.EncodeTSOption(2, reply.TSVal, options[:])
				ack.Options = options[:n+header.AddTCPOptionPadding(options[:], n)]
			}
			conn.Send(t, ack)
			dut.PollOne(t, listenFD, unix.POLLIN, time.Second)
			fd, _ := dut.Accept(t, listenFD)
			t.Cleanup(func() { dut.Close(t, fd) })

			first := *conn.LocalSeqNum(t)
			observed := 0
			expectACK := func(offset uint32, wantBlocks ...header.SACKBlock) {
				t.Helper()
				got, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(first) + offset)}, time.Second)
				if err != nil {
					t.Fatalf("waiting for ACK at offset %d: %v", offset, err)
				}
				observed++
				blocks := header.ParseTCPOptions(got.Options).SACKBlocks
				var ranges []string
				for _, block := range blocks {
					ranges = append(ranges, fmt.Sprintf("%d-%d", uint32(block.Start)-uint32(first), uint32(block.End)-uint32(first)))
				}
				fmt.Printf("COOKIE_SACK case=%s step=%d ack=%d blocks=%v\n", tc.name, observed, *got.AckNum-uint32(first), ranges)
				if diff := cmp.Diff(wantBlocks, header.ParseTCPOptions(got.Options).SACKBlocks); diff != "" {
					t.Fatalf("SACK blocks at ACK offset %d differ (-want +got):\n%s", offset, diff)
				}
			}
			conn.Send(t, ack, &testbench.Payload{Bytes: []byte("a")})
			expectACK(1)

			// Leave one byte missing. The cumulative ACK remains at the
			// hole; only a peer that negotiated SACK learns about 'c'.
			ack.SeqNum = testbench.Uint32(uint32(first.Add(2)))
			conn.Send(t, ack, &testbench.Payload{Bytes: []byte("c")})
			if wantSACK {
				expectACK(1, header.SACKBlock{Start: first.Add(2), End: first.Add(3)})
			} else {
				expectACK(1)
			}
			ack.SeqNum = testbench.Uint32(uint32(first.Add(1)))
			conn.Send(t, ack, &testbench.Payload{Bytes: []byte("b")})
			expectACK(3)
		})
	}
}

func init() {
	testbench.Initialize(flag.CommandLine)
}

func sendSYN(t *testing.T, dut *testbench.DUT, toListener testbench.TCP, options []byte) (*testbench.TCPIPv4, header.TCPSynOptions) {
	t.Helper()
	conn := dut.Net.NewTCPIPv4(t, toListener, testbench.TCP{SrcPort: toListener.DstPort})
	t.Cleanup(func() { conn.Close(t) })
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn), Options: options})
	reply, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn | header.TCPFlagAck)}, time.Second)
	if err != nil {
		t.Fatalf("waiting for SYN-ACK: %v", err)
	}
	return &conn, header.ParseSynOptions(reply.Options, true /* isAck */)
}

// fillSYNQueue returns a stateful connection and the first one using a cookie.
// The SYNs must offer SACK without timestamps: like gVisor, Linux omits SACK
// from a cookie SYN-ACK when the peer hasn't offered timestamps:
// https://github.com/torvalds/linux/blob/adc218676/net/ipv4/tcp_input.c#L7232-L7239
func fillSYNQueue(t *testing.T, dut *testbench.DUT, toListener testbench.TCP, options []byte) (*testbench.TCPIPv4, *testbench.TCPIPv4) {
	t.Helper()
	stateful, first := sendSYN(t, dut, toListener, options)
	if !first.SACKPermitted || (header.ParseSynOptions(options, false /* isAck */).WS >= 0 && first.WS < 0) {
		t.Fatalf("first SYN-ACK did not negotiate the offered options: %+v", first)
	}
	// Keep each SYN outstanding. Linux and gVisor can save different
	// numbers of half-open connections for the same listen backlog.
	for range 16 {
		conn, reply := sendSYN(t, dut, toListener, options)
		if !reply.SACKPermitted {
			return stateful, conn
		}
	}
	t.Fatal("listener never switched to SYN cookies")
	return nil, nil
}

// TestTCPSynCookie checks which final handshake flags a listener accepts
// for a saved SYN and for a SYN cookie.
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

			listenFD, remotePort := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1 /* backlog */)
			defer dut.Close(t, listenFD)

			var options [4]byte
			n := header.EncodeSACKPermittedOption(options[:])
			header.AddTCPOptionPadding(options[:], n)
			withoutSynCookieConn, withSynCookieConn := fillSYNQueue(t, &dut, testbench.TCP{DstPort: &remotePort}, options[:])

			for _, c := range []struct {
				desc             string
				conn             *testbench.TCPIPv4
				expectRetransmit bool
			}{
				{desc: "without syncookies", conn: withoutSynCookieConn, expectRetransmit: true},
				{desc: "with syncookies", conn: withSynCookieConn, expectRetransmit: false},
			} {
				t.Run(c.desc, func(t *testing.T) {
					retransmit := testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn | header.TCPFlagAck)}
					retransmit.SeqNum = testbench.Uint32(uint32(*c.conn.RemoteSeqNum(t) - 1))
					got, err := c.conn.Expect(t, retransmit, 2*time.Second)
					if c.expectRetransmit && err != nil {
						t.Fatalf("expected retransmitted SYNACK, but got %s", err)
					}
					if !c.expectRetransmit && err == nil {
						t.Fatalf("expected no retransmitted SYNACK, but got %s", got)
					}
				})
			}

			// Check whether ACKs with the given flags completes the handshake.
			for _, c := range []struct {
				desc string
				conn *testbench.TCPIPv4
			}{
				{desc: "with syncookies", conn: withSynCookieConn},
				{desc: "without syncookies", conn: withoutSynCookieConn},
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
