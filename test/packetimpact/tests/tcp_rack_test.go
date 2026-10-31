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

// TestSuccessiveRepairSACKs holds a single hole open while the peer
// extends, reorders and duplicates reports of two later repairs.
func TestSuccessiveRepairSACKs(t *testing.T) {
	const mode = "successive"
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
	const flight = 10
	sendAndReceive(t, dut, conn, flight, fd, mss, false)
	time.Sleep(simulatedRTT)
	ack(mss, 2*mss, 5*mss)
	packet("fast_repair", mss, mss, time.Second)
	if got := observe("fast_recovery").CaState; got != uint8(linux.TCP_CA_Recovery) {
		t.Fatalf("after fast repair: state = %d, want recovery", got)
	}
	packet("first_timeout", mss, mss, 4*time.Second)
	loss := observe("first_timeout")
	if loss.CaState != uint8(linux.TCP_CA_Loss) {
		t.Fatalf("after timeout: state = %d, want loss", loss.CaState)
	}

	// Restore the previous SACK, then cumulatively ACK it with the repair
	// of segment 2. This retires the old SACK before either fresh report.
	ack(mss, 2*mss, 5*mss)
	ack(5*mss, -1, -1)
	end := time.Now().Add(time.Duration(loss.RTO) * time.Microsecond / 2)
	packet("six", 5*mss, mss, time.Until(end))
	packet("seven", 6*mss, mss, time.Until(end))
	ack(7*mss, -1, -1)
	start := time.Now()
	packet("eight", 7*mss, mss, time.Until(end))
	packet("nine", 8*mss, mss, time.Until(end))
	packet("ten", 9*mss, mss, time.Until(end))
	first := observe("before_feedback")
	rto := time.Duration(first.RTO) * time.Microsecond

	// Both later transmissions are already on the wire. Withhold #8 and
	// hold #9's ACK long enough to distinguish its two transmissions.
	// A subsequent SACK of #10 can refer only to the earlier batch.
	window := min(rto/8, 50*time.Millisecond)
	collect := func(phase string, duration time.Duration) {
		frames, found := conn.ListenForFrame(t, testbench.Layers{nil, nil,
			&testbench.TCP{SeqNum: testbench.Uint32(uint32(seq((flight + 1) * mss)))},
			&testbench.Payload{Bytes: make([]byte, mss)},
		}, duration)
		if found {
			t.Fatal("received unsent sequence")
		}
		for _, f := range frames {
			if len(f) != 4 {
				continue
			}
			tcp, ok := f[2].(*testbench.TCP)
			data, hasData := f[3].(*testbench.Payload)
			if ok && hasData && tcp.SeqNum != nil && len(data.Bytes) > 0 {
				fmt.Printf("FENCE_WIRE mode=%s phase=%s offset=%d bytes=%d\n", mode, phase, uint32(*tcp.SeqNum)-uint32(base), len(data.Bytes))
			}
		}
		observe(phase)
	}
	collect("before_first", min(rto/4, 80*time.Millisecond))
	ack(7*mss, 8*mss, 9*mss)
	collect("first", window)
	ack(7*mss, 8*mss, 10*mss)
	collect("extended", window)
	ack(7*mss, 8*mss, 9*mss)
	collect("stale", window/2)
	ack(7*mss, 8*mss, 10*mss)
	collect("duplicate", window/2)
	if elapsed := time.Since(start); elapsed >= rto {
		t.Fatalf("observation reached the RTO: elapsed %v, RTO %v", elapsed, rto)
	}
	fmt.Printf("FENCE_DONE mode=%s elapsed_us=%d rto_us=%d\n", mode, time.Since(start).Microseconds(), rto.Microseconds())
}
