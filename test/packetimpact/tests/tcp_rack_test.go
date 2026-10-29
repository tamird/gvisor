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

// TestFlightFence SACKs the last old packet before sending new data. The two
// receivers differ only in whether that new data reaches the out-of-order queue.
func TestFlightFence(t *testing.T) {
	for _, mode := range []string{"sack_old", "sack_old_and_new"} {
		t.Run(mode, func(t *testing.T) {
			dut, conn, fd, mss := createSACKConnection(t)
			dut.SetSockOpt(t, fd, unix.IPPROTO_TCP, unix.TCP_CONGESTION, []byte("reno"))
			dut.SetSockOptInt(t, fd, unix.IPPROTO_TCP, unix.TCP_NODELAY, 1)
			sendAndReceive(t, dut, conn, 4, fd, mss, true)
			base := *conn.RemoteSeqNum(t)
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
			send := func(at string, off, size int) {
				if got, want := dut.Send(t, fd, make([]byte, size), 0), int32(size); got != want {
					t.Fatalf("Send = %d, want %d", got, want)
				}
				packet(at, off, size, time.Second)
			}
			const guard = 3 * time.Second
			old := 7*mss + 3
			sendAndReceive(t, dut, conn, 7, fd, mss, false)
			for off := old - 3; off < old; off++ {
				send("original_tail", off, 1)
			}
			time.Sleep(simulatedRTT)
			ack(mss, 4*mss, 7*mss)
			packet("fast_repair", mss, mss, guard)
			if got, want := observe("fast_repair").CaState, uint8(linux.TCP_CA_Recovery); got != want {
				t.Fatalf("first repair state=%d want recovery (%d)", got, want)
			}
			packet("first_timeout", mss, mss, guard)
			if got, want := observe("first_timeout").CaState, uint8(linux.TCP_CA_Loss); got != want {
				t.Fatalf("second repair state=%d want loss (%d)", got, want)
			}
			ack(2*mss, 4*mss, 7*mss)
			packet("partial_repair", 2*mss, 0, guard)
			ack(old-3, old-1, old)
			packet("tail_repair", old-3, 0, guard)
			info := observe("before_queue")
			if got, want := dut.Send(t, fd, make([]byte, mss), 0), int32(mss); got != want {
				t.Fatalf("Send(new write) = %d, want %d", got, want)
			}
			// The peer has already buffered the last of the three old packets.
			// The two earlier packets remain outstanding, leaving room for the
			// new segment if the sender uses its third congestion-window slot.
			window := min(80*time.Millisecond, time.Duration(info.RTO)*time.Microsecond/3)
			_, sent := conn.ListenForFrame(t, testbench.Layers{nil, nil,
				&testbench.TCP{SeqNum: testbench.Uint32(uint32(seq(old)))},
				&testbench.Payload{Bytes: make([]byte, mss)},
			}, window)
			fmt.Printf("FENCE_WIRE mode=%s phase=admission new_sent=%t window_us=%d\n", mode, sent, window.Microseconds())
			if !sent {
				t.Fatal("no new segment before the second timeout; cannot construct the peer SACK")
			}
			started := time.Now()
			before := observe("new_outstanding")
			packet("second_timeout", old-3, 1, guard)
			if elapsed := time.Since(started); elapsed < time.Duration(before.RTO)*time.Microsecond/2 {
				t.Fatalf("not an RTO witness: repair after %v, RTO=%d us", elapsed, before.RTO)
			}
			observe("second_timeout")
			// Deliver the first old byte just retransmitted, keep the middle
			// old byte missing, and report the buffered final old byte. If the
			// new segment arrived too, it is contiguous with that final byte.
			sackEnd := old
			if mode == "sack_old_and_new" {
				sackEnd += mss
			}
			ack(old-2, old-1, sackEnd)
			time.Sleep(simulatedRTT)
			observe("after_partial_ack")
			// Do not require an immediate repair: whether and when the sender
			// sends it is one outcome under comparison. A second full timeout
			// lies outside this bounded observation.
			frames, found := conn.ListenForFrame(t, testbench.Layers{nil, nil,
				&testbench.TCP{SeqNum: testbench.Uint32(uint32(seq(old - 2)))},
				&testbench.Payload{Bytes: make([]byte, 1)},
			}, window)
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
				fmt.Printf("FENCE_WIRE mode=%s phase=after_partial_ack offset=%d bytes=%d\n", mode, uint32(*tcp.SeqNum)-uint32(base), len(payload.Bytes))
			}
			fmt.Printf("FENCE_WIRE mode=%s phase=missing_repair found=%t window_us=%d\n", mode, found, window.Microseconds())
			fmt.Printf("FENCE_DONE mode=%s\n", mode)
		})
	}
}
