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

package ip_test

import (
	"bytes"
	"testing"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/network/internal/ip"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

const (
	testTCPHdrLen = header.TCPMinimumSize + 12 // with 12 bytes of options
	testSeqNum    = 0xfffffc00                 // overflows during segmentation
	testFlags     = header.TCPFlagAck | header.TCPFlagPsh | header.TCPFlagFin | header.TCPFlagCwr
)

func makeTCPAggregate(t *testing.T, payload []byte, parseTransportHeader bool) *stack.PacketBuffer {
	t.Helper()

	tcpHdr := header.TCP(make([]byte, testTCPHdrLen))
	tcpHdr.Encode(&header.TCPFields{
		SrcPort:    1234,
		DstPort:    80,
		SeqNum:     testSeqNum,
		AckNum:     200,
		DataOffset: testTCPHdrLen,
		Flags:      testFlags,
		WindowSize: 65535,
		Checksum:   0xabcd,
	})
	for i := header.TCPMinimumSize; i < testTCPHdrLen; i++ {
		tcpHdr[i] = byte(i) // "options" that must be copied to every segment
	}

	pkt := stack.NewPacketBuffer(stack.PacketBufferOptions{
		Payload: buffer.MakeWithData(append(append([]byte(nil), tcpHdr...), payload...)),
	})
	if parseTransportHeader {
		if _, ok := pkt.TransportHeader().Consume(testTCPHdrLen); !ok {
			t.Fatal("failed to consume the transport header")
		}
	}
	return pkt
}

func TestTCPSegmenter(t *testing.T) {
	const (
		mss     = 500
		reserve = 60
	)
	payload := make([]byte, 1200) // 3 segments: 500, 500, 200
	for i := range payload {
		payload[i] = byte(i)
	}

	for _, parseTransportHeader := range []bool{false, true} {
		name := "transport header in data"
		if parseTransportHeader {
			name = "transport header parsed"
		}
		t.Run(name, func(t *testing.T) {
			pkt := makeTCPAggregate(t, payload, parseTransportHeader)
			defer pkt.DecRef()

			ts, ok := ip.MakeTCPSegmenter(pkt, mss, testTCPHdrLen+mss, reserve)
			if !ok {
				t.Fatal("MakeTCPSegmenter(...) = (_, false), want (_, true)")
			}
			defer ts.Release()

			wantSizes := []int{500, 500, 200}
			var gotPayload []byte
			for i, wantSize := range wantSizes {
				if got, want := ts.RemainingSegmentCount(), len(wantSizes)-i; got != want {
					t.Errorf("segment %d: ts.RemainingSegmentCount() = %d, want = %d", i, got, want)
				}
				segPkt, more := ts.BuildNextSegment()
				if got, want := more, i != len(wantSizes)-1; got != want {
					t.Errorf("segment %d: BuildNextSegment() = (_, %t), want (_, %t)", i, got, want)
				}

				tcpHdr := header.TCP(segPkt.TransportHeader().Slice())
				if got, want := len(tcpHdr), testTCPHdrLen; got != want {
					t.Fatalf("segment %d: len(tcpHdr) = %d, want = %d", i, got, want)
				}
				for j := header.TCPMinimumSize; j < testTCPHdrLen; j++ {
					if tcpHdr[j] != byte(j) {
						t.Errorf("segment %d: option byte %d = %x, want = %x", i, j, tcpHdr[j], byte(j))
					}
				}
				if got, want := tcpHdr.SequenceNumber(), uint32(testSeqNum+len(gotPayload)); got != want {
					t.Errorf("segment %d: tcpHdr.SequenceNumber() = %d, want = %d", i, got, want)
				}
				wantFlags := testFlags
				if i != len(wantSizes)-1 {
					wantFlags &^= header.TCPFlagFin | header.TCPFlagPsh
				}
				if i != 0 {
					wantFlags &^= header.TCPFlagCwr
				}
				if got := tcpHdr.Flags(); got != wantFlags {
					t.Errorf("segment %d: tcpHdr.Flags() = %s, want = %s", i, got, wantFlags)
				}
				if got := tcpHdr.Checksum(); got != 0 {
					t.Errorf("segment %d: tcpHdr.Checksum() = %x, want = 0", i, got)
				}

				if got, want := segPkt.Data().Size(), wantSize; got != want {
					t.Errorf("segment %d: segPkt.Data().Size() = %d, want = %d", i, got, want)
				}
				if got, want := segPkt.AvailableHeaderBytes(), reserve; got != want {
					t.Errorf("segment %d: segPkt.AvailableHeaderBytes() = %d, want = %d", i, got, want)
				}
				data := segPkt.Data().ToBuffer()
				gotPayload = append(gotPayload, data.Flatten()...)
				data.Release()
				segPkt.DecRef()
			}
			if !bytes.Equal(gotPayload, payload) {
				t.Error("the segments' payloads do not reassemble to the original payload")
			}
		})
	}
}

func TestTCPSegmenterRejects(t *testing.T) {
	tests := []struct {
		name              string
		payloadSize       int
		mss               int
		maxSegmentPayload int
	}{
		{
			name:              "segments would not fit",
			payloadSize:       1200,
			mss:               500,
			maxSegmentPayload: testTCPHdrLen + 499,
		},
		{
			name:              "no need to split",
			payloadSize:       400,
			mss:               500,
			maxSegmentPayload: testTCPHdrLen + 500,
		},
		{
			name:              "zero mss",
			payloadSize:       1200,
			mss:               0,
			maxSegmentPayload: testTCPHdrLen + 500,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			pkt := makeTCPAggregate(t, make([]byte, test.payloadSize), false)
			defer pkt.DecRef()
			if _, ok := ip.MakeTCPSegmenter(pkt, test.mss, test.maxSegmentPayload, 0); ok {
				t.Fatal("MakeTCPSegmenter(...) = (_, true), want (_, false)")
			}
		})
	}

	t.Run("truncated TCP header", func(t *testing.T) {
		pkt := stack.NewPacketBuffer(stack.PacketBufferOptions{
			Payload: buffer.MakeWithData(make([]byte, header.TCPMinimumSize-1)),
		})
		defer pkt.DecRef()
		if _, ok := ip.MakeTCPSegmenter(pkt, 500, 1000, 0); ok {
			t.Fatal("MakeTCPSegmenter(...) = (_, true), want (_, false)")
		}
	})
}
