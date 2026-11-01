// Copyright 2026 The gVisor Authors.
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

package ip

import (
	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

// TCPSegmenter splits the payload of a TCP segmentation offload aggregate
// into individual TCP packets no larger than the size the original sender
// used, replicating the aggregate's TCP header into each with the required
// per-segment fixups. It is the network-layer equivalent of Linux's
// tcp_gso_segment(), for use when forwarding an aggregate that is larger
// than the outgoing link's MTU.
type TCPSegmenter struct {
	payload        buffer.Buffer
	hdr            []byte
	mss            int
	reserve        int
	mark           uint32
	seq            uint32
	segmentCount   int
	currentSegment int
}

// MakeTCPSegmenter prepares a TCPSegmenter for the TCP packet in pkt, whose
// network header must already be parsed and whose TCP header and payload
// make up its transport header and data sections.
//
// mss is the payload size to use for each produced segment.
// maxSegmentPayload is the maximum network-layer payload (TCP header plus
// data) each produced segment may have. reserve is the number of bytes to
// reserve in each produced packet for the network and link headers.
//
// ok is false if mss-sized segments would exceed maxSegmentPayload, if the
// TCP header cannot be parsed, or if the payload does not actually need to
// be split; in that case the packet cannot be segmented and the caller
// should fall back to its non-GSO behavior.
//
// If ok is true, the caller is responsible for calling Release on the
// returned segmenter.
func MakeTCPSegmenter(pkt *stack.PacketBuffer, mss int, maxSegmentPayload int, reserve int) (TCPSegmenter, bool) {
	var data buffer.Buffer
	data.Append(pkt.TransportHeader().View())
	pktBuf := pkt.Data().ToBuffer()
	data.Merge(&pktBuf)

	v, ok := data.PullUp(0, header.TCPMinimumSize)
	if !ok {
		data.Release()
		return TCPSegmenter{}, false
	}
	hdrLen := int(header.TCP(v.AsSlice()).DataOffset())
	if hdrLen < header.TCPMinimumSize || mss <= 0 || hdrLen+mss > maxSegmentPayload {
		data.Release()
		return TCPSegmenter{}, false
	}
	hv, ok := data.PullUp(0, hdrLen)
	if !ok {
		data.Release()
		return TCPSegmenter{}, false
	}
	hdr := make([]byte, hdrLen)
	copy(hdr, hv.AsSlice())
	data.TrimFront(int64(hdrLen))
	payloadLen := int(data.Size())
	if payloadLen <= mss {
		data.Release()
		return TCPSegmenter{}, false
	}

	return TCPSegmenter{
		payload:      data,
		hdr:          hdr,
		mss:          mss,
		reserve:      reserve,
		mark:         pkt.Mark,
		seq:          header.TCP(hdr).SequenceNumber(),
		segmentCount: (payloadLen + mss - 1) / mss,
	}, true
}

// BuildNextSegment returns a packet carrying the next mss-sized run of
// payload, along with a boolean indicating if more segments remain. If this
// function is called again after it indicated that no more segments were
// left, it will panic.
//
// The returned packet has its transport header and data populated. Its
// checksum field is zeroed: the caller must compute it once the pseudo
// header is known. The network and link headers are not populated, but
// space for them is reserved.
func (s *TCPSegmenter) BuildNextSegment() (*stack.PacketBuffer, bool) {
	if s.currentSegment >= s.segmentCount {
		panic("BuildNextSegment should not be called again after the last segment was returned")
	}

	segPkt := stack.NewPacketBuffer(stack.PacketBufferOptions{
		ReserveHeaderBytes: s.reserve + len(s.hdr),
		Mark:               s.mark,
	})
	copied := segPkt.Data().ReadFrom(&s.payload, s.mss)
	tcpHdr := header.TCP(segPkt.TransportHeader().Push(len(s.hdr)))
	copy(tcpHdr, s.hdr)
	segPkt.TransportProtocolNumber = header.TCPProtocolNumber

	s.currentSegment++
	more := s.currentSegment != s.segmentCount

	tcpHdr.SetSequenceNumber(s.seq)
	s.seq += uint32(copied)
	flags := tcpHdr.Flags()
	if more {
		// FIN and PSH describe the end of the aggregated byte run, so they
		// only apply to the segment carrying its final bytes.
		flags &^= header.TCPFlagFin | header.TCPFlagPsh
	}
	if s.currentSegment != 1 {
		// CWR must only be signaled once, on the first segment.
		flags &^= header.TCPFlagCwr
	}
	tcpHdr.SetFlags(uint8(flags))
	tcpHdr.SetChecksum(0)

	return segPkt, more
}

// RemainingSegmentCount returns the number of segments left to be built.
func (s *TCPSegmenter) RemainingSegmentCount() int {
	return s.segmentCount - s.currentSegment
}

// Release frees resources owned by the segmenter.
func (s *TCPSegmenter) Release() {
	s.payload.Release()
}
