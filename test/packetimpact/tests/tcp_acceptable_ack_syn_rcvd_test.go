// Copyright 2022 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//	http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
package tcp_acceptable_ack_syn_rcvd_test

import (
	"flag"
	"fmt"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/test/packetimpact/testbench"
)

func init() {
	testbench.Initialize(flag.CommandLine)
}

func TestAcceptableAckInSynRcvd(t *testing.T) {
	for _, tt := range []struct {
		offset    uint32
		expectRst bool
	}{
		{offset: 0, expectRst: true},
		// The ACK holds the next expected SEQ so valid segments must hold an ACK
		// that is 1 larger than the last SEQ value.
		{offset: 1, expectRst: false},
		{offset: 2, expectRst: true},
	} {
		t.Run(fmt.Sprintf("offset=%d, expectRst=%t", tt.offset, tt.expectRst), func(t *testing.T) {
			dut := testbench.NewDUT(t)
			listenFd, listenerPort := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
			defer dut.Close(t, listenFd)
			conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &listenerPort}, testbench.TCP{SrcPort: &listenerPort})
			defer conn.Close(t)

			conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn)})

			synAck, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn | header.TCPFlagAck)}, time.Second)
			if err != nil {
				t.Fatalf("didn't get synack during handshake: %s", err)
			}

			// Calculate the ACK number.
			ackNum := *synAck.SeqNum + tt.offset
			conn.Send(t, testbench.TCP{AckNum: &ackNum, Flags: testbench.TCPFlags(header.TCPFlagAck)})

			if tt.expectRst {
				if _, err := conn.Expect(t, testbench.TCP{SeqNum: &ackNum, Flags: testbench.TCPFlags(header.TCPFlagRst)}, time.Second); err != nil {
					t.Fatalf("failed to receive rst for an unacceptable ack: %s", err)
				}
			} else {
				acceptFd, _ := dut.Accept(t, listenFd)
				dut.Close(t, acceptFd)
			}
		})
	}
}

func TestRepeatedSynInSynRcvd(t *testing.T) {
	dut := testbench.NewDUT(t)
	listener, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
	defer dut.Close(t, listener)
	conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &port}, testbench.TCP{SrcPort: &port})
	defer conn.Close(t)

	syn := conn.CreateFrame(t, testbench.Layers{&testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn)}})
	// Both replies must precede the initial one-second retransmission timeout.
	deadline := time.Now().Add(500 * time.Millisecond)
	conn.SendFrame(t, syn)
	flags := testbench.TCPFlags(header.TCPFlagSyn | header.TCPFlagAck)
	first, err := conn.Expect(t, testbench.TCP{Flags: flags}, time.Until(deadline))
	if err != nil {
		t.Fatalf("initial SYN-ACK: %s", err)
	}

	conn.SendFrameStateless(t, syn)
	if _, err := conn.Expect(t, testbench.TCP{Flags: flags, SeqNum: first.SeqNum, AckNum: first.AckNum}, time.Until(deadline)); err != nil {
		t.Fatalf("SYN-ACK after repeated SYN: %s", err)
	}
	// An immediate third SYN should be rate limited.
	conn.SendFrameStateless(t, syn)
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck)})
	child, _ := dut.Accept(t, listener)
	defer dut.Close(t, child)
	if got, want := dut.GetSockOptTCPInfo(t, child).TotalRetrans, uint32(1); got != want {
		t.Errorf("accepted socket total retransmissions = %d, want %d", got, want)
	}
}

// Fork control: move the requested retry far enough from the original SYN
// to tell the old timeout from a new timeout at the same backoff level.
func TestRepeatedSynRenewsDeadline(t *testing.T) {
	dut := testbench.NewDUT(t)
	listener, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
	defer dut.Close(t, listener)
	conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &port}, testbench.TCP{SrcPort: &port})
	defer conn.Close(t)
	opts := make([]byte, header.TCPOptionTSLength)
	header.EncodeTSOption(1100, 0, opts)
	syn := conn.CreateFrame(t, testbench.Layers{&testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn), Options: opts}})
	start := time.Now()
	conn.SendFrame(t, syn)
	flags := testbench.TCPFlags(header.TCPFlagSyn | header.TCPFlagAck)
	first, err := conn.Expect(t, testbench.TCP{Flags: flags}, 300*time.Millisecond)
	if err != nil {
		t.Fatalf("initial SYN-ACK: %s", err)
	}
	time.Sleep(time.Until(start.Add(550 * time.Millisecond)))
	// Linux ignores ECE/CWR when matching the original peer SYN.
	last := syn[len(syn)-1].(*testbench.TCP)
	header.EncodeTSOption(1200, 0, opts)
	last.Options = opts
	last.Flags = testbench.TCPFlags(header.TCPFlagSyn | header.TCPFlagEce | header.TCPFlagCwr)
	repeated := time.Now()
	conn.SendFrameStateless(t, syn)
	want := testbench.TCP{Flags: flags, SeqNum: first.SeqNum, AckNum: first.AckNum}
	retry, err := conn.Expect(t, want, time.Until(start.Add(875*time.Millisecond)))
	if err != nil {
		t.Fatalf("SYN-ACK after ECN-marked repeat: %s", err)
	}
	immediate := time.Now()
	timer, err := conn.Expect(t, want, time.Until(repeated.Add(1900*time.Millisecond)))
	if err != nil {
		t.Fatalf("timed SYN-ACK: %s", err)
	}
	elapsed := time.Since(repeated)
	t.Logf("DUPSYN_TIMER before_repeat_us=%d requested_reply_us=%d timed_reply_us=%d", repeated.Sub(start).Microseconds(), immediate.Sub(repeated).Microseconds(), elapsed.Microseconds())
	if elapsed < 800*time.Millisecond || elapsed > 1900*time.Millisecond {
		t.Fatalf("timed response %s after the repeated SYN", elapsed)
	}
	initialTS := header.ParseSynOptions(first.Options, true)
	retryTS := header.ParseSynOptions(retry.Options, true)
	timerTS := header.ParseSynOptions(timer.Options, true)
	header.EncodeTSOption(1300, timerTS.TSVal, opts)
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), Options: opts})
	child, _ := dut.Accept(t, listener)
	defer dut.Close(t, child)
	info := dut.GetSockOptTCPInfo(t, child)
	t.Logf("DUPSYN_TS initial=%d:%d requested=%d:%d timed=%d:%d total=%d child_rtt_us=%d", initialTS.TSVal, initialTS.TSEcr, retryTS.TSVal, retryTS.TSEcr, timerTS.TSVal, timerTS.TSEcr, info.TotalRetrans, info.RTT)
	if !initialTS.TS || !retryTS.TS || !timerTS.TS || initialTS.TSEcr != 1100 || retryTS.TSEcr != 1100 || timerTS.TSEcr != 1100 {
		t.Errorf("timestamp option/echo changed: first=%+v requested=%+v timed=%+v", initialTS, retryTS, timerTS)
	}
	if int32(retryTS.TSVal-initialTS.TSVal) <= 0 || int32(timerTS.TSVal-retryTS.TSVal) <= 0 {
		t.Errorf("reused local send timestamp: first=%d requested=%d timed=%d", initialTS.TSVal, retryTS.TSVal, timerTS.TSVal)
	}
	if got := info.TotalRetrans; got != 2 {
		t.Errorf("accepted socket total retransmissions = %d, want 2", got)
	}
}

func TestStaleRepeatedSynGetsACK(t *testing.T) {
	dut := testbench.NewDUT(t)
	listener, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
	defer dut.Close(t, listener)
	conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &port}, testbench.TCP{SrcPort: &port})
	defer conn.Close(t)
	opts := make([]byte, header.TCPOptionTSLength)
	header.EncodeTSOption(11, 0, opts)
	syn := conn.CreateFrame(t, testbench.Layers{&testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn), Options: opts}})
	conn.SendFrame(t, syn)
	first, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn | header.TCPFlagAck)}, 300*time.Millisecond)
	if err != nil {
		t.Fatalf("initial SYN-ACK: %s", err)
	}
	ts := header.ParseSynOptions(first.Options, true)
	if !ts.TS || ts.TSEcr != 11 {
		t.Fatalf("timestamps not negotiated: %+v", ts)
	}
	header.EncodeTSOption(10, 0, opts)
	syn[len(syn)-1].(*testbench.TCP).Options = opts
	conn.SendFrameStateless(t, syn)
	seq := *first.SeqNum + 1
	if _, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), SeqNum: &seq, AckNum: first.AckNum}, 200*time.Millisecond); err != nil {
		t.Fatalf("out-of-window ACK for a stale timestamp: %s", err)
	}
	header.EncodeTSOption(12, ts.TSVal, opts)
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), Options: opts})
	child, _ := dut.Accept(t, listener)
	defer dut.Close(t, child)
	if got := dut.GetSockOptTCPInfo(t, child).TotalRetrans; got != 0 {
		t.Errorf("accepted socket total retransmissions = %d, want 0", got)
	}
}

func TestDuplicateSynTimestampHalfSpace(t *testing.T) {
	dut := testbench.NewDUT(t)
	listener, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
	defer dut.Close(t, listener)
	conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &port}, testbench.TCP{SrcPort: &port})
	defer conn.Close(t)
	opts := make([]byte, header.TCPOptionTSLength)
	header.EncodeTSOption(11, 0, opts)
	syn := conn.CreateFrame(t, testbench.Layers{&testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn), Options: opts}})
	conn.SendFrame(t, syn)
	synACK := header.TCPFlagSyn | header.TCPFlagAck
	first, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(synACK)}, 300*time.Millisecond)
	if err != nil {
		t.Fatalf("first SYN-ACK: %s", err)
	}
	ts := header.ParseSynOptions(first.Options, true)
	if !ts.TS || ts.TSEcr != 11 {
		t.Fatalf("timestamps not negotiated: %+v", ts)
	}
	header.EncodeTSOption((1<<31)+11, 0, opts)
	syn[len(syn)-1].(*testbench.TCP).Options = opts
	conn.SendFrameStateless(t, syn)
	frames, _ := conn.ListenForFrame(t, testbench.Layers{nil, nil, &testbench.TCP{Flags: testbench.TCPFlags(synACK), SeqNum: first.SeqNum}}, 200*time.Millisecond)
	var response string
	for _, frame := range frames {
		for _, layer := range frame {
			p, ok := layer.(*testbench.TCP)
			if !ok || *p.SrcPort != *first.SrcPort || *p.DstPort != *first.DstPort {
				continue
			}
			if *p.AckNum != *first.AckNum {
				t.Fatalf("unexpected ACK number: %+v", p)
			}
			switch {
			case *p.Flags == synACK && *p.SeqNum == *first.SeqNum:
				response = "SYN-ACK"
			case *p.Flags == header.TCPFlagAck && *p.SeqNum == *first.SeqNum+1:
				response = "ACK"
			default:
				t.Fatalf("unexpected reply: %+v", p)
			}
			break
		}
		if response != "" {
			break
		}
	}
	header.EncodeTSOption(12, ts.TSVal, opts)
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), Options: opts})
	child, _ := dut.Accept(t, listener)
	defer dut.Close(t, child)
	total := dut.GetSockOptTCPInfo(t, child).TotalRetrans
	t.Logf("DUPSYN_HALF previous=11 peer=2147483659 response=%s total=%d", response, total)
	if response != "SYN-ACK" || total != 1 {
		t.Errorf("half-space timestamp got %q and %d child retries; want SYN-ACK and 1", response, total)
	}
}
