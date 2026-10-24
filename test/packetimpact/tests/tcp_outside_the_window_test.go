// Copyright 2020 The gVisor Authors.
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

package tcp_outside_the_window_test

import (
	"flag"
	"fmt"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/test/packetimpact/testbench"
)

func init() {
	testbench.Initialize(flag.CommandLine)
}

func TestWindowWithUnreadFIN(t *testing.T) {
	const queuedData = "queued data"
	for _, tc := range []struct {
		name    string
		inside  seqnum.Size
		packets string
		read    string
	}{
		{"AtEdge", 0, "FINOnly", "None"},
		{"OverlapsEdge", 1, "FINOnly", "None"},
		{"TogetherExact", 0, "Together", "Exact"},
		{"TogetherOversized", 0, "Together", "Oversized"},
		{"TogetherPeek", 0, "Together", "Peek"},
		{"SeparateExact", 0, "Separate", "Exact"},
		{"SeparateOversized", 0, "Separate", "Oversized"},
		{"SeparatePeek", 0, "Separate", "Peek"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dut := testbench.NewDUT(t)
			listenFD, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
			defer dut.Close(t, listenFD)
			conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &port}, testbench.TCP{SrcPort: &port})
			defer conn.Close(t)
			conn.Connect(t)
			fd, _ := dut.Accept(t, listenFD)
			defer dut.Close(t, fd)

			ack := testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck)}
			var finPayload []testbench.Layer
			if tc.packets == "Together" {
				finPayload = []testbench.Layer{&testbench.Payload{Bytes: []byte(queuedData)}}
			} else if tc.packets == "Separate" {
				conn.Send(t, ack, &testbench.Payload{Bytes: []byte(queuedData)})
				if _, err := conn.Expect(t, ack, time.Second); err != nil {
					t.Fatalf("no ACK for queued data before FIN: %v", err)
				}
			}
			conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagFin | header.TCPFlagAck)}, finPayload...)
			reply, err := conn.Expect(t, ack, time.Second)
			if err != nil {
				t.Fatalf("no ACK for FIN: %v", err)
			}
			if got, want := uint32(dut.GetSockOptTCPInfo(t, fd).State), uint32(linux.TCP_CLOSE_WAIT); got != want {
				t.Fatalf("TCP state after FIN = %d, want %d", got, want)
			}

			if tc.read != "None" {
				readSize := int32(len(queuedData))
				readFlags := int32(unix.MSG_DONTWAIT)
				if tc.read == "Oversized" {
					readSize++
				} else if tc.read == "Peek" {
					readFlags |= unix.MSG_PEEK
				}
				if got, want := string(dut.Recv(t, fd, readSize, readFlags)), queuedData; got != want {
					t.Fatalf("queued data read = %q, want %q", got, want)
				}
				// Record any read-induced window update before sending the probe.
				// Separate wire packets may still coalesce in Linux's receive queue.
				for updates := 0; ; updates++ {
					update, err := conn.Expect(t, ack, 100*time.Millisecond)
					if err != nil {
						break
					}
					reply = update
					t.Logf("read window update: %s", update)
					if updates >= 3 {
						t.Fatal("more than four read-induced ACKs")
					}
				}
			}
			window := seqnum.Size(*reply.WindowSize)
			if window == 0 {
				t.Fatal("peer advertised a zero window")
			}
			t.Logf("DUT %s window %d", dut.Uname.KernelRelease, window)

			// Inject two bytes across the advertised edge without advancing
			// either side of the test connection.
			seq := conn.LocalSeqNum(t).Add(window - tc.inside)
			conn.SendFrameStateless(t, conn.CreateFrame(t, testbench.Layers{
				&testbench.TCP{SeqNum: testbench.Uint32(uint32(seq)), Flags: ack.Flags},
			}, &testbench.Payload{Bytes: []byte("xy")}))
			frames, acknowledged := conn.ListenForFrame(t, testbench.Layers{nil, nil, &ack}, time.Second)
			t.Logf("READ_BOUNDARY packets=%s read=%s inside=%d ack=%t frames=%v", tc.packets, tc.read, tc.inside, acknowledged, frames)
			if tc.packets == "FINOnly" && !acknowledged {
				t.Errorf("no ACK for the unchanged receive sequence")
			}
			// The new boundary cases observe ACK polarity rather than assuming
			// that separate wire packets imply separate queued segments.
			if tc.read == "Peek" {
				if got, want := string(dut.Recv(t, fd, int32(len(queuedData)+1), unix.MSG_DONTWAIT)), queuedData; got != want {
					t.Errorf("queued data after peek/probe = %q, want %q", got, want)
				}
			}
			if got := dut.Recv(t, fd, 1, unix.MSG_DONTWAIT); len(got) != 0 {
				t.Errorf("received %q after FIN, want EOF", got)
			}
			if got, want := uint32(dut.GetSockOptTCPInfo(t, fd).State), uint32(linux.TCP_CLOSE_WAIT); got != want {
				t.Errorf("TCP state after probe = %d, want %d", got, want)
			}
		})
	}
}

// TestTCPOutsideTheWindows tests the behavior of the DUT when packets arrive
// that are inside or outside the TCP window. Packets that are outside the
// window should force an extra ACK, as described in RFC793 page 69:
// https://tools.ietf.org/html/rfc793#page-69
func TestTCPOutsideTheWindow(t *testing.T) {
	for _, tt := range []struct {
		description  string
		tcpFlags     header.TCPFlags
		payload      []testbench.Layer
		seqNumOffset seqnum.Size
		expectACK    bool
	}{
		{"SYN", header.TCPFlagSyn, nil, 0, true},
		{"SYNACK", header.TCPFlagSyn | header.TCPFlagAck, nil, 0, true},
		{"ACK", header.TCPFlagAck, nil, 0, false},
		{"FIN", header.TCPFlagFin, nil, 0, false},
		{"Data", header.TCPFlagAck, []testbench.Layer{&testbench.Payload{Bytes: []byte("abc123")}}, 0, true},

		{"SYN", header.TCPFlagSyn, nil, 1, true},
		{"SYNACK", header.TCPFlagSyn | header.TCPFlagAck, nil, 1, true},
		{"ACK", header.TCPFlagAck, nil, 1, true},
		{"FIN", header.TCPFlagFin, nil, 1, false},
		{"Data", header.TCPFlagAck, []testbench.Layer{&testbench.Payload{Bytes: []byte("abc123")}}, 1, true},

		{"SYN", header.TCPFlagSyn, nil, 2, true},
		{"SYNACK", header.TCPFlagSyn | header.TCPFlagAck, nil, 2, true},
		{"ACK", header.TCPFlagAck, nil, 2, true},
		{"FIN", header.TCPFlagFin, nil, 2, false},
		{"Data", header.TCPFlagAck, []testbench.Layer{&testbench.Payload{Bytes: []byte("abc123")}}, 2, true},
	} {
		t.Run(fmt.Sprintf("%s%d", tt.description, tt.seqNumOffset), func(t *testing.T) {
			dut := testbench.NewDUT(t)
			listenFD, remotePort := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
			defer dut.Close(t, listenFD)
			conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &remotePort}, testbench.TCP{SrcPort: &remotePort})
			defer conn.Close(t)
			conn.Connect(t)
			acceptFD, _ := dut.Accept(t, listenFD)
			defer dut.Close(t, acceptFD)

			windowSize := seqnum.Size(*conn.SynAck(t).WindowSize) + tt.seqNumOffset
			conn.Drain(t)
			// Ignore whatever incrementing that this out-of-order packet might cause
			// to the AckNum.
			localSeqNum := testbench.Uint32(uint32(*conn.LocalSeqNum(t)))
			conn.Send(t, testbench.TCP{
				Flags:  testbench.TCPFlags(tt.tcpFlags),
				SeqNum: testbench.Uint32(uint32(conn.LocalSeqNum(t).Add(windowSize))),
			}, tt.payload...)
			timeout := time.Second
			gotACK, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: localSeqNum}, timeout)
			if tt.expectACK && err != nil {
				t.Fatalf("expected an ACK packet within %s but got none: %s", timeout, err)
			}
			// Data packets w/o SYN bits are always acked by Linux. Netstack ACK's data packets
			// always right now. So only send a second segment and test for no ACK for packets
			// with no data.
			if tt.expectACK && tt.payload == nil {
				// Sending another out-of-window segment immediately should not trigger
				// an ACK if less than 500ms(default rate limit for out-of-window ACKs)
				// has passed since the last ACK was sent.
				t.Logf("sending another segment")
				conn.Send(t, testbench.TCP{
					Flags:  testbench.TCPFlags(tt.tcpFlags),
					SeqNum: testbench.Uint32(uint32(conn.LocalSeqNum(t).Add(windowSize))),
				}, tt.payload...)
				timeout := 3 * time.Second
				gotACK, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: localSeqNum}, timeout)
				if err == nil {
					t.Fatalf("expected no ACK packet but got one: %s", gotACK)
				}
			}
			if !tt.expectACK && gotACK != nil {
				t.Fatalf("expected no ACK packet within %s but got one: %s", timeout, gotACK)
			}
		})
	}
}

// TestAckOTWSeqInClosing tests that the DUT should send an ACK with
// the right ACK number when receiving a packet with OTW Seq number
// in CLOSING state. https://tools.ietf.org/html/rfc793#page-69
func TestAckOTWSeqInClosing(t *testing.T) {
	for _, tt := range []struct {
		description  string
		flags        header.TCPFlags
		payloads     testbench.Layers
		seqNumOffset seqnum.Size
		expectACK    bool
	}{
		{"SYN", header.TCPFlagSyn, nil, 0, true},
		{"SYNACK", header.TCPFlagSyn | header.TCPFlagAck, nil, 0, true},
		{"ACK", header.TCPFlagAck, nil, 0, false},
		{"FINACK", header.TCPFlagFin | header.TCPFlagAck, nil, 0, false},
		{"Data", header.TCPFlagAck, []testbench.Layer{&testbench.Payload{Bytes: []byte("Sample Data")}}, 0, false},

		{"SYN", header.TCPFlagSyn, nil, 1, true},
		{"SYNACK", header.TCPFlagSyn | header.TCPFlagAck, nil, 1, true},
		{"ACK", header.TCPFlagAck, nil, 1, true},
		{"FINACK", header.TCPFlagFin | header.TCPFlagAck, nil, 1, true},
		{"Data", header.TCPFlagAck, []testbench.Layer{&testbench.Payload{Bytes: []byte("Sample Data")}}, 1, true},

		{"SYN", header.TCPFlagSyn, nil, 2, true},
		{"SYNACK", header.TCPFlagSyn | header.TCPFlagAck, nil, 2, true},
		{"ACK", header.TCPFlagAck, nil, 2, true},
		{"FINACK", header.TCPFlagFin | header.TCPFlagAck, nil, 2, true},
		{"Data", header.TCPFlagAck, []testbench.Layer{&testbench.Payload{Bytes: []byte("Sample Data")}}, 2, true},
	} {
		t.Run(fmt.Sprintf("%s%d", tt.description, tt.seqNumOffset), func(t *testing.T) {
			dut := testbench.NewDUT(t)
			listenFD, remotePort := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
			defer dut.Close(t, listenFD)
			conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &remotePort}, testbench.TCP{SrcPort: &remotePort})
			defer conn.Close(t)
			conn.Connect(t)
			acceptFD, _ := dut.Accept(t, listenFD)
			defer dut.Close(t, acceptFD)

			dut.Shutdown(t, acceptFD, unix.SHUT_WR)

			if _, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagFin | header.TCPFlagAck)}, time.Second); err != nil {
				t.Fatalf("expected FINACK from DUT, but got none: %s", err)
			}

			// Do not ack the FIN from DUT so that the TCP state on DUT is CLOSING instead of CLOSED.
			seqNumForTheirFIN := testbench.Uint32(uint32(*conn.RemoteSeqNum(t)) - 1)
			conn.Send(t, testbench.TCP{AckNum: seqNumForTheirFIN, Flags: testbench.TCPFlags(header.TCPFlagFin | header.TCPFlagAck)})

			gotTCP, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck)}, time.Second)
			if err != nil {
				t.Fatalf("expected an ACK to our FIN, but got none: %s", err)
			}

			windowSize := seqnum.Size(*gotTCP.WindowSize) + tt.seqNumOffset
			conn.SendFrameStateless(t, conn.CreateFrame(t, testbench.Layers{&testbench.TCP{
				SeqNum: testbench.Uint32(uint32(conn.LocalSeqNum(t).Add(windowSize))),
				AckNum: seqNumForTheirFIN,
				Flags:  testbench.TCPFlags(tt.flags),
			}}, tt.payloads...))

			gotACK, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck)}, time.Second)
			if tt.expectACK && err != nil {
				t.Errorf("expected an ACK but got none: %s", err)
			}
			if !tt.expectACK && gotACK != nil {
				t.Errorf("expected no ACK but got one: %s", gotACK)
			}
		})
	}
}
