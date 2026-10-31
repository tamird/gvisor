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

package tcp

import (
	"testing"

	"github.com/google/go-cmp/cmp"
	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

const trueSegSize = stack.PacketBufferStructSize + segSize

type segmentSizeWants struct {
	DataSize   int
	SegMemSize int
}

func checkSegmentSize(t *testing.T, name string, seg *segment, want segmentSizeWants) {
	t.Helper()
	got := segmentSizeWants{
		DataSize:   seg.payloadSize(),
		SegMemSize: seg.segMemSize(),
	}
	if diff := cmp.Diff(want, got); diff != "" {
		t.Errorf("%s differs (-want +got):\n%s", name, diff)
	}
}

func TestSegmentMerge(t *testing.T) {
	var clock faketime.NullClock
	id := stack.TransportEndpointID{}
	seg1 := newOutgoingSegment(id, &clock, buffer.MakeWithView(buffer.NewViewSize(10)), 0)
	defer seg1.DecRef()
	seg2 := newOutgoingSegment(id, &clock, buffer.MakeWithView(buffer.NewViewSize(20)), 0)
	defer seg2.DecRef()

	checkSegmentSize(t, "seg1", seg1, segmentSizeWants{
		DataSize:   10,
		SegMemSize: trueSegSize + 10,
	})
	checkSegmentSize(t, "seg2", seg2, segmentSizeWants{
		DataSize:   20,
		SegMemSize: trueSegSize + 20,
	})

	seg1.merge(seg2)

	checkSegmentSize(t, "seg1", seg1, segmentSizeWants{
		DataSize:   30,
		SegMemSize: trueSegSize + 30,
	})
	checkSegmentSize(t, "seg2", seg2, segmentSizeWants{
		DataSize:   0,
		SegMemSize: trueSegSize,
	})
}

func incomingControl(t *testing.T, flags header.TCPFlags, payloadSize int) *segment {
	t.Helper()
	const tcpHeaderSize = header.TCPMinimumSize + 12
	packetSize := header.IPv4MinimumSize + tcpHeaderSize + payloadSize
	src := tcpip.AddrFrom4([4]byte{192, 0, 2, 1})
	dst := tcpip.AddrFrom4([4]byte{192, 0, 2, 2})
	pkt := stack.NewPacketBuffer(stack.PacketBufferOptions{
		Payload: buffer.MakeWithView(buffer.NewViewSize(packetSize)),
	})
	defer pkt.DecRef()
	pkt.NetworkProtocolNumber = header.IPv4ProtocolNumber
	pkt.TransportProtocolNumber = header.TCPProtocolNumber
	pkt.NICID = 7

	netBytes, ok := pkt.NetworkHeader().Consume(header.IPv4MinimumSize)
	if !ok {
		t.Fatal("failed to consume IPv4 header")
	}
	ip := header.IPv4(netBytes)
	ip.Encode(&header.IPv4Fields{
		TotalLength: uint16(packetSize),
		TTL:         64,
		Protocol:    uint8(header.TCPProtocolNumber),
		SrcAddr:     src,
		DstAddr:     dst,
	})
	ip.SetChecksum(^ip.CalculateChecksum())
	tcpBytes, ok := pkt.TransportHeader().Consume(tcpHeaderSize)
	if !ok {
		t.Fatal("failed to consume TCP header")
	}
	tcpHeader := header.TCP(tcpBytes)
	tcpHeader.Encode(&header.TCPFields{
		SrcPort:    1234,
		DstPort:    5678,
		SeqNum:     100,
		AckNum:     200,
		DataOffset: tcpHeaderSize,
		Flags:      flags,
		WindowSize: 30000,
	})
	opts := tcpBytes[header.TCPMinimumSize:]
	opts[0], opts[1] = header.TCPOptionNOP, header.TCPOptionNOP
	header.EncodeTSOption(123, 456, opts[2:])
	// The zero-filled payload contributes zero to the checksum.
	xsum := header.PseudoHeaderChecksum(header.TCPProtocolNumber, src, dst, uint16(tcpHeaderSize+payloadSize))
	tcpHeader.SetChecksum(^tcpHeader.CalculateChecksum(xsum))
	var clock faketime.NullClock
	s, err := newIncomingSegment(stack.TransportEndpointID{
		LocalAddress:  dst,
		LocalPort:     5678,
		RemoteAddress: src,
		RemotePort:    1234,
	}, &clock, pkt)
	if err != nil {
		t.Fatalf("newIncomingSegment: %s", err)
	}
	if !s.csumValid {
		s.DecRef()
		t.Fatal("incoming segment checksum is invalid")
	}
	return s
}

// TestIngressControlBudget records which packets the receive queue admits while
// its worker is paused. A dequeued reference represents data retained by the app.
func TestIngressControlBudget(t *testing.T) {
	const budget = 4096
	const burst = 257
	var ep Endpoint
	q := segmentQueue{ep: &ep}
	ep.ops.SetReceiveBufferSize(budget, false)
	ep.ops.SetSendBufferSize(budget, false)
	offer := func(flags header.TCPFlags, payload int) bool {
		s := incomingControl(t, flags, payload)
		defer s.DecRef()
		return q.enqueue(s)
	}
	drain := func() {
		for s := q.dequeue(); s != nil; s = q.dequeue() {
			s.DecRef()
		}
	}
	defer drain()

	for _, payload := range []int{0, budget, 8 * budget} {
		var held *segment
		if payload != 0 {
			ep.ops.SetReceiveBufferSize(int64(2*payload), false)
			if !offer(header.TCPFlagAck, payload) {
				t.Fatal("initial data refused")
			}
			held = q.dequeue()
			ep.ops.SetReceiveBufferSize(budget, false)
		}
		before := ep.receiveMemUsed()
		data := false
		if held != nil {
			data = offer(header.TCPFlagAck, 1)
			if data {
				t.Fatal("accepted extra data while over the receive budget")
			}
		}
		accepted := 0
		for i := 0; i < burst; i++ {
			if offer(header.TCPFlagAck, 0) {
				accepted++
			}
		}
		peak := ep.receiveMemUsed()
		drain()
		if got, want := ep.receiveMemUsed(), before; got != want {
			t.Fatalf("draining ACKs left %d bytes charged, want %d", got, want)
		}
		for _, flags := range []header.TCPFlags{header.TCPFlagFin | header.TCPFlagAck, header.TCPFlagRst} {
			if !offer(flags, 0) {
				t.Fatalf("refused control %#x with empty backlog, retained payload %d", flags, payload)
			}
			drain()
		}
		q.freeze()
		if offer(header.TCPFlagAck, 0) {
			t.Fatal("frozen queue accepted an ACK")
		}
		q.thaw()
		if held != nil {
			held.DecRef()
		}
		if got, want := ep.receiveMemUsed(), 0; got != want {
			t.Fatalf("final charge = %d, want %d", got, want)
		}
		t.Logf("INGRESS payload=%d accepted=%d before=%d peak=%d", payload, accepted, before, peak)
	}
}
