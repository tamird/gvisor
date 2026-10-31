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
	"math"
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

// TestLargeReceiveCharge models a full SO_RCVBUFFORCE socket without
// allocating two GiB. enqueue accounts the actual final segment and decides
// whether a subsequent payload fits.
func TestLargeReceiveCharge(t *testing.T) {
	var ep Endpoint
	ep.ops.SetReceiveBufferSize(math.MaxInt32, false)
	ep.updateReceiveMemUsed(math.MaxInt32 - 1)
	defer ep.updateReceiveMemUsed(-(math.MaxInt32 - 1))
	q := segmentQueue{ep: &ep}
	defer func() {
		for s := q.dequeue(); s != nil; s = q.dequeue() {
			s.DecRef()
		}
	}()
	enqueue := func() (int, bool) {
		s := incomingControl(t, header.TCPFlagAck, 1)
		defer s.DecRef()
		return s.segMemSize(), q.enqueue(s)
	}
	if got, want := ep.receiveBufferAvailable(), 1; got != want {
		t.Fatalf("available before enqueue: got %d, want %d", got, want)
	}
	size, ok := enqueue()
	if !ok {
		t.Fatal("queue rejected the first packet despite one byte of headroom")
	}
	used, free := ep.receiveMemUsed(), ep.receiveBufferAvailable()
	_, extra := enqueue()
	t.Logf("WINDOW charge=%d used=%d free=%d extra=%t", size, used, free, extra)
}
