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

package tcp_retransmits_test

import (
	"bytes"
	"flag"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/test/packetimpact/testbench"
)

func init() {
	testbench.Initialize(flag.CommandLine)
}

func getRTO(t *testing.T, dut testbench.DUT, acceptFd int32) (rto time.Duration) {
	info := dut.GetSockOptTCPInfo(t, acceptFd)
	return time.Duration(info.RTO) * time.Microsecond
}

// TestRetransmits tests retransmits occur at exponentially increasing
// time intervals.
func TestRetransmits(t *testing.T) {
	dut := testbench.NewDUT(t)
	listenFd, remotePort := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
	defer dut.Close(t, listenFd)
	conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &remotePort}, testbench.TCP{SrcPort: &remotePort})
	defer conn.Close(t)

	conn.Connect(t)
	acceptFd, _ := dut.Accept(t, listenFd)
	defer dut.Close(t, acceptFd)

	dut.SetSockOptInt(t, acceptFd, unix.IPPROTO_TCP, unix.TCP_NODELAY, 1)

	sampleData := []byte("Sample Data")
	samplePayload := &testbench.Payload{Bytes: sampleData}

	// Give a chance for the dut to estimate RTO with RTT from the DATA-ACK.
	// This is to reduce the test run-time from the default initial RTO of 1s.
	// TODO(gvisor.dev/issue/2685) Estimate RTO during handshake, after which
	// we can skip this data send/recv which is solely to estimate RTO.
	dut.Send(t, acceptFd, sampleData, 0)
	if _, err := conn.ExpectData(t, &testbench.TCP{}, samplePayload, time.Second); err != nil {
		t.Fatalf("expected payload was not received: %s", err)
	}
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck | header.TCPFlagPsh)}, samplePayload)
	if _, err := conn.ExpectData(t, &testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck)}, nil, time.Second); err != nil {
		t.Fatalf("expected packet was not received: %s", err)
	}
	// Wait for the DUT to receive the data, thus ensuring that the stack has
	// estimated RTO before we query RTO via TCP_INFO.
	if got := dut.Recv(t, acceptFd, int32(len(sampleData)), 0); !bytes.Equal(got, sampleData) {
		t.Fatalf("got dut.Recv(t, %d, %d, 0) = %s, want %s", acceptFd, len(sampleData), got, sampleData)
	}

	const timeoutCorrection = time.Second
	const diffCorrection = 200 * time.Millisecond
	rto := getRTO(t, dut, acceptFd)

	dut.Send(t, acceptFd, sampleData, 0)
	seq := testbench.Uint32(uint32(*conn.RemoteSeqNum(t)))
	if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: seq}, samplePayload, rto+timeoutCorrection); err != nil {
		t.Fatalf("expected payload was not received: %s", err)
	}

	// Expect retransmits of the same segment.
	for i := 0; i < 5; i++ {
		startTime := time.Now()
		rto = getRTO(t, dut, acceptFd)
		if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: seq}, samplePayload, rto+timeoutCorrection); err != nil {
			t.Fatalf("expected payload was not received within %s loop %d err %s", rto+timeoutCorrection, i, err)
		}
		if diff := time.Since(startTime); diff+diffCorrection < rto {
			t.Fatalf("retransmit came sooner got: %s want: >= %s probe %d", diff+diffCorrection, rto, i)
		}
	}
}

// TestIdleRestart checks the first flight after the sender stops sending data.
// Reverse traffic elicits pure ACKs without advancing the forward ACK clock.
func TestIdleRestart(t *testing.T) {
	for _, mode := range []string{"quiet", "reverse"} {
		t.Run(mode, func(t *testing.T) {
			dut := testbench.NewDUT(t)
			listener, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
			defer dut.Close(t, listener)
			conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &port, WindowSize: testbench.Uint16(65535)}, testbench.TCP{SrcPort: &port})
			defer conn.Close(t)
			// Use a modest MSS so flow control cannot limit the flight. Leave
			// SACK disabled so a tail-loss probe cannot add to the burst.
			options := make([]byte, 4)
			header.EncodeMSSOption(536, options)
			conn.ConnectWithOptions(t, options)
			fd, _ := dut.Accept(t, listener)
			defer dut.Close(t, fd)
			dut.SetSockOptInt(t, fd, unix.IPPROTO_TCP, unix.TCP_NODELAY, 1)
			initial := dut.GetSockOptTCPInfo(t, fd)
			iw, mss := int(initial.SndCwnd), int(initial.SndMss)
			if iw < 2 || mss != 536 || 6*iw*mss >= 65535 {
				t.Fatalf("unexpected initial window: cwnd=%d MSS=%d", iw, mss)
			}

			// Count contiguous observed bytes, including a possible aggregate
			// packet. The peer acknowledges only bytes it actually saw.
			send := func(data []byte) {
				t.Helper()
				if n := dut.Send(t, fd, data, 0); int(n) != len(data) {
					t.Fatalf("Send = %d, want %d", n, len(data))
				}
			}
			nextData := func(data []byte) int {
				t.Helper()
				seq := uint32(*conn.RemoteSeqNum(t))
				frame, err := conn.ExpectData(t, &testbench.TCP{SeqNum: &seq}, nil, time.Second)
				if err != nil || len(frame) != 4 {
					t.Fatalf("data at seq=%d: frame=%v err=%v", seq, frame, err)
				}
				payload := frame[3].(*testbench.Payload).Bytes
				if len(payload) == 0 || len(payload) > len(data) || !bytes.Equal(payload, data[:len(payload)]) {
					t.Fatalf("invalid data at seq=%d: %d bytes, want <= %d", seq, len(payload), len(data))
				}
				return len(payload)
			}
			receive := func(data []byte, ack bool) {
				t.Helper()
				for len(data) > 0 {
					data = data[nextData(data):]
					if ack {
						conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck)})
					}
				}
			}
			forward := bytes.Repeat([]byte{0x31}, 4*iw*mss)
			send(forward)
			receive(forward, true)

			// Wait for an exact pure ACK to our one-byte marker. Its forward
			// sequence is the end of the warmup; its ACK covers the new byte.
			reverse := func() {
				t.Helper()
				seq := uint32(*conn.RemoteSeqNum(t))
				b := &testbench.Payload{Bytes: []byte{'r'}}
				conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck | header.TCPFlagPsh)}, b)
				ack := uint32(*conn.LocalSeqNum(t))
				frame, err := conn.ExpectData(t, &testbench.TCP{SeqNum: &seq, AckNum: &ack, Flags: testbench.TCPFlags(header.TCPFlagAck)}, nil, time.Second)
				if err != nil || len(frame) != 3 {
					t.Fatalf("expected pure ACK seq=%d ack=%d: %v, %v", seq, ack, frame, err)
				}
			}
			reverse()
			warm := dut.GetSockOptTCPInfo(t, fd)
			rto := time.Duration(warm.RTO) * time.Microsecond
			if warm.SndCwnd < 2*initial.SndCwnd || warm.SndCwnd >= 6*initial.SndCwnd || rto < 50*time.Millisecond || rto > 2*time.Second {
				t.Fatalf("warmup failed: cwnd=%d IW=%d RTO=%v", warm.SndCwnd, iw, rto)
			}
			start := time.Now()
			lastACK := start
			var maxACKGap time.Duration
			reverseACKs := 0
			if mode == "quiet" {
				time.Sleep(6 * rto)
			} else {
				for time.Since(start) < 6*rto {
					time.Sleep(rto / 5)
					reverse()
					now := time.Now()
					maxACKGap = max(maxACKGap, now.Sub(lastACK))
					lastACK = now
					reverseACKs++
				}
			}
			before := dut.GetSockOptTCPInfo(t, fd)
			idle, lastGap := time.Since(start), time.Since(lastACK)
			maxACKGap = max(maxACKGap, lastGap)
			if before.SndCwnd != warm.SndCwnd || idle < 6*rto || (mode == "reverse" && (maxACKGap >= rto || reverseACKs < 6)) {
				t.Fatalf("invalid idle interval: warm=%d before=%d idle=%v ACKs=%d maxGap=%v RTO=%v", warm.SndCwnd, before.SndCwnd, idle, reverseACKs, maxACKGap, rto)
			}

			// Keep the receiver window open and withhold every ACK until
			// both TCP_INFO and the actual contiguous first flight are read.
			resume := bytes.Repeat([]byte{0x52}, 6*iw*mss)
			clock := time.Now()
			send(resume)
			first := nextData(resume)
			after := dut.GetSockOptTCPInfo(t, fd)
			flight := int(after.SndCwnd) * mss
			if flight < first || flight >= len(resume) {
				t.Fatalf("unusable resumed cwnd=%d (queued %d bytes)", after.SndCwnd, len(resume))
			}
			receive(resume[first:flight], false)
			seq := uint32(*conn.RemoteSeqNum(t))
			if frame, err := conn.ExpectData(t, &testbench.TCP{SeqNum: &seq}, nil, rto/5); err == nil {
				t.Fatalf("extra packet before ACK after %d bytes: %v", flight, frame)
			}
			if time.Since(clock) >= rto {
				t.Fatalf("observation crossed retransmit deadline %v", rto)
			}
			conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck)})
			receive(resume[flight:], true)
			t.Logf("IDLE_WIRE mode=%s iw=%d mss=%d warm=%d before=%d resumed=%d flight=%d rto_us=%d idle_us=%d reverse_acks=%d max_ack_gap_us=%d last_ack_gap_us=%d", mode, iw, mss, warm.SndCwnd, before.SndCwnd, after.SndCwnd, flight, rto.Microseconds(), idle.Microseconds(), reverseACKs, maxACKGap.Microseconds(), lastGap.Microseconds())
			if after.SndCwnd != initial.SndCwnd {
				t.Errorf("resumed congestion window = %d, want initial %d", after.SndCwnd, iw)
			}
		})
	}
}
