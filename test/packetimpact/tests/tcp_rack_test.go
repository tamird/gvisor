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

package tcp_rack_test

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

const (
	// simulatedRTT is the time delay between packets sent and acked to
	// increase the RTT.
	simulatedRTT = 30 * time.Millisecond

	// numPktsForRTT is the number of packets sent and acked to establish
	// RTT.
	numPktsForRTT = 10
)

func createSACKConnection(t *testing.T) (testbench.DUT, testbench.TCPIPv4, int32, int) {
	t.Helper()
	dut := testbench.NewDUT(t)
	listenFd, remotePort := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
	t.Cleanup(func() { dut.Close(t, listenFd) })
	conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &remotePort}, testbench.TCP{SrcPort: &remotePort})
	t.Cleanup(func() { conn.Close(t) })

	// Enable SACK.
	opts := make([]byte, 40)
	optsOff := 0
	optsOff += header.EncodeNOP(opts[optsOff:])
	optsOff += header.EncodeNOP(opts[optsOff:])
	optsOff += header.EncodeSACKPermittedOption(opts[optsOff:])

	conn.ConnectWithOptions(t, opts[:optsOff])
	acceptFd, _ := dut.Accept(t, listenFd)
	t.Cleanup(func() { dut.Close(t, acceptFd) })

	// Each write must occupy one full segment: the recovery assertions count
	// packets and construct SACK ranges from their payloads. TCP options can
	// reduce the usable payload below the negotiated MSS.
	payloadSize := int(dut.GetSockOptTCPInfo(t, acceptFd).SndMss)
	if payloadSize <= 0 {
		t.Fatalf("TCP_INFO send MSS = %d, want positive", payloadSize)
	}
	return dut, conn, acceptFd, payloadSize
}

func getRTTAndRTO(t *testing.T, dut testbench.DUT, acceptFd int32) (rtt, rto time.Duration) {
	info := dut.GetSockOptTCPInfo(t, acceptFd)
	return time.Duration(info.RTT) * time.Microsecond, time.Duration(info.RTO) * time.Microsecond
}

func sendAndReceive(t *testing.T, dut testbench.DUT, conn testbench.TCPIPv4, numPkts int, acceptFd int32, payloadSize int, sendACK bool) time.Time {
	t.Helper()
	seqNum1 := *conn.RemoteSeqNum(t)
	payload := make([]byte, payloadSize)
	var lastSent time.Time
	for i, sn := 0, seqNum1; i < numPkts; i++ {
		lastSent = time.Now()
		if got, want := dut.Send(t, acceptFd, payload, 0), int32(len(payload)); got != want {
			t.Fatalf("Send #%d = %d, want %d", i+1, got, want)
		}
		if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(sn))}, &testbench.Payload{Bytes: payload}, time.Second); err != nil {
			t.Fatalf("ExpectData #%d: %s", i+1, err)
		}
		sn.UpdateForward(seqnum.Size(payloadSize))

		if sendACK {
			time.Sleep(simulatedRTT)
			conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(sn))})
		}
	}
	return lastSent
}

// TestLostTimeoutRepair drops a second repair after a timeout interrupted SACK recovery.
func TestLostTimeoutRepair(t *testing.T) {
	for _, mode := range []string{"repeat_ack", "sack_repair"} {
		t.Run(mode, func(t *testing.T) {
			dut, conn, fd, mss := createSACKConnection(t)
			dut.SetSockOpt(t, fd, unix.IPPROTO_TCP, unix.TCP_CONGESTION, []byte("reno"))
			dut.SetSockOptInt(t, fd, unix.IPPROTO_TCP, unix.TCP_NODELAY, 1)
			sendAndReceive(t, dut, conn, 4, fd, mss, true)
			base := *conn.RemoteSeqNum(t)
			fmt.Printf("FENCE_FLOW mode=%s base=%d\n", mode, uint32(base))
			seq := func(off int) seqnum.Value { return base.Add(seqnum.Size(off)) }
			observe := func(at string) linux.TCPInfo {
				info := dut.GetSockOptTCPInfo(t, fd)
				fmt.Printf("FENCE_STATE mode=%s phase=%s ca=%d cwnd=%d threshold=%d rto_us=%d retransmits=%d backoff=%d mss=%d\n", mode, at, info.CaState, info.SndCwnd, info.SndSsthresh, info.RTO, info.Retransmits, info.Backoff, mss)
				return info
			}
			ack := func(off, start, end int) {
				var opts []byte
				if start >= 0 {
					opts = make([]byte, 40)
					n := header.EncodeNOP(opts)
					n += header.EncodeNOP(opts[n:])
					n += header.EncodeSACKBlocks([]header.SACKBlock{{Start: seq(start), End: seq(end)}}, opts[n:])
					opts = opts[:n]
				}
				conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(seq(off))), Options: opts})
				fmt.Printf("FENCE_ACK mode=%s cumulative=%d sack=%d-%d\n", mode, off, start, end)
			}
			packet := func(at string, off, size int, timeout time.Duration) {
				t.Helper()
				var payload testbench.Payload
				if size > 0 {
					payload.Bytes = make([]byte, size)
				}
				start := time.Now()
				layers, err := conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(seq(off)))}, &payload, timeout)
				if err != nil {
					t.Fatalf("%s, offset=%d: %v", at, off, err)
				}
				actual := len(layers[len(layers)-1].(*testbench.Payload).Bytes)
				fmt.Printf("FENCE_WIRE mode=%s phase=%s offset=%d bytes=%d wait_us=%d\n", mode, at, off, actual, time.Since(start).Microseconds())
			}
			const flight = 8
			sendAndReceive(t, dut, conn, flight, fd, mss, false)
			time.Sleep(simulatedRTT)
			// This non-contiguous block remains in the receiver through the
			// timeout. Receiving the fast repair, then the timeout repair of the
			// same hole, distinguishes an interrupted recovery from a first RTO.
			ack(mss, 4*mss, 7*mss)
			packet("fast_repair", mss, mss, time.Second)
			if got, want := observe("before_timeout").CaState, uint8(linux.TCP_CA_Recovery); got != want {
				t.Fatalf("state after fast repair = %d, want Recovery (%d)", got, want)
			}
			packet("first_timeout", mss, mss, 4*time.Second)
			loss := observe("first_timeout")
			if got, want := loss.CaState, uint8(linux.TCP_CA_Loss); got != want {
				t.Fatalf("state after first timeout = %d, want Loss (%d)", got, want)
			}
			ack(2*mss, 4*mss, 7*mss)
			// Discard the repair of segment 3, accept the repair of segment 4,
			// then hold its SACK longer than the measured minimum RTT. Both
			// repairs must arrive well before a second retransmission timeout.
			stageEnd := time.Now().Add(time.Duration(loss.RTO)*time.Microsecond/2)
			packet("discarded_repair", 2*mss, mss, time.Until(stageEnd))
			packet("later_repair", 3*mss, mss, time.Until(stageEnd))
			repair := observe("before_feedback")
			// The short-delay SACK reached gVisor before its minimum RTT.
			// Hold this one past that minimum; check the sample in the sender.
			const delay = 80 * time.Millisecond
			if delay >= time.Duration(repair.RTO)*time.Microsecond/3 {
				t.Fatalf("insufficient time before the retransmission deadline: %+v", repair)
			}
			time.Sleep(delay)
			if mode == "repeat_ack" {
				ack(2*mss, 4*mss, 7*mss)
			} else {
				ack(2*mss, 3*mss, 7*mss)
			}
			// Wait for an impossible sequence so ListenForFrame records every
			// repair. End this observation well before another RTO is due.
			rto := time.Duration(repair.RTO) * time.Microsecond
			window := min(80*time.Millisecond, rto/4)
			if window < time.Millisecond {
				t.Fatalf("unusable RTO: %v", rto)
			}
			start := time.Now()
			frames, found := conn.ListenForFrame(t, testbench.Layers{nil, nil,
				&testbench.TCP{SeqNum: testbench.Uint32(uint32(seq((flight + 1) * mss)))},
				&testbench.Payload{Bytes: make([]byte, mss)},
			}, window)
			if found {
				t.Fatal("received unsent sequence")
			}
			early := false
			for _, frame := range frames {
				if len(frame) != 4 {
					continue
				}
				tcp, ok := frame[2].(*testbench.TCP)
				if !ok || tcp.SeqNum == nil {
					continue
				}
				payload, ok := frame[3].(*testbench.Payload)
				if !ok || len(payload.Bytes) == 0 {
					continue
				}
				off := uint32(*tcp.SeqNum) - uint32(base)
				if off == uint32(2*mss) {
					early = true
				}
				if off < uint32(flight*mss) {
					fmt.Printf("FENCE_WIRE mode=%s phase=after_ack offset=%d bytes=%d\n", mode, off, len(payload.Bytes))
				}
			}
			observe("after_ack")
			if elapsed := time.Since(start); elapsed >= rto {
				t.Fatalf("observation lasted %v, reached next timeout %v", elapsed, rto)
			}
			if !early {
				packet("later_retry", 2*mss, mss, 4*time.Second)
				observe("later_retry")
			}
			fmt.Printf("FENCE_DONE mode=%s\n", mode)
		})
	}
}
