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

func TestSynCookieWindowScale(t *testing.T) {
	for _, timestamps := range []bool{false, true} {
		t.Run(fmt.Sprintf("timestamps=%t", timestamps), func(t *testing.T) {
			dut := testbench.NewDUT(t)
			listenFD, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1 /* backlog */)
			t.Cleanup(func() { dut.Close(t, listenFD) })

			const peerShift = 3
			const windowField = 16
			toListener := testbench.TCP{DstPort: &port, WindowSize: testbench.Uint16(windowField)}
			var options [header.TCPOptionsMaximumSize]byte
			n := header.EncodeSACKPermittedOption(options[:])
			n += header.EncodeWSOption(peerShift, options[n:])
			fillSYNQueue(t, &dut, toListener, options[:n+header.AddTCPOptionPadding(options[:], n)])
			if timestamps {
				n += header.EncodeTSOption(1, 0, options[n:])
			}
			n += header.AddTCPOptionPadding(options[:], n)
			conn, opts := sendSYN(t, &dut, toListener, options[:n])
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
			// Fill the window first: Linux need not split a queued sub-MSS
			// write that is one byte larger than this tiny window.
			if got := dut.Send(t, fd, data[:wantWindow], 0); got != int32(wantWindow) {
				t.Fatalf("send accepted %d bytes, want %d", got, wantWindow)
			}
			if _, err := conn.ExpectNextData(t, &testbench.TCP{}, &testbench.Payload{Bytes: data[:wantWindow]}, time.Second); err != nil {
				t.Fatalf("expected %d bytes before the first ACK: %v", wantWindow, err)
			}
			tail := &testbench.Payload{Bytes: data[wantWindow:]}
			if got := dut.Send(t, fd, tail.Bytes, 0); got != 1 {
				t.Fatalf("send accepted %d tail bytes, want 1", got)
			}
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
