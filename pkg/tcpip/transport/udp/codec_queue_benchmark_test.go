// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package udp

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

func releaseCodecQueue(queue *udpPacketList) {
	for !queue.Empty() {
		packet := queue.Front()
		queue.Remove(packet)
		packet.pkt.DecRef()
	}
}

// BenchmarkUDPReceiveQueueSave serializes the actual receive-list subtree.
// The chosen occupancies are scenarios, not measured production frequencies.
// Packet delivery, endpoint freezing and the rest of the kernel are not timed.
func BenchmarkUDPReceiveQueueSave(b *testing.B) {
	benchmarkUDPReceiveQueueSave(b, state.SaveSnapshots)
}

func benchmarkUDPReceiveQueueSave(b *testing.B, save func(context.Context, io.Writer, any) (state.Stats, error)) {
	b.Helper()
	for _, count := range []int{1, 8, 32} {
		b.Run(fmt.Sprint(count), func(b *testing.B) {
			var queue udpPacketList
			b.Cleanup(func() { releaseCodecQueue(&queue) })
			remote := tcpip.AddrFrom4([4]byte{192, 0, 2, 1})
			local := tcpip.AddrFrom4([4]byte{192, 0, 2, 2})
			timestamp := time.Date(2026, time.October, 7, 12, 0, 0, 1, time.UTC)
			memory := 0
			for i := range count {
				// Match the existing UDP tests' 30-byte payload size. Build
				// parsed incoming headers, as retained by HandlePacket.
				data := make([]byte, header.IPv4MinimumSize+header.UDPMinimumSize+30)
				ip := header.IPv4(data)
				ip.Encode(&header.IPv4Fields{
					TotalLength: uint16(len(data)), TTL: 65, TOS: 0x80,
					Protocol: uint8(header.UDPProtocolNumber), SrcAddr: remote, DstAddr: local,
				})
				ip.SetChecksum(^ip.CalculateChecksum())
				header.UDP(data[header.IPv4MinimumSize:]).Encode(&header.UDPFields{
					SrcPort: 1234, DstPort: 4321, Length: header.UDPMinimumSize + 30,
				})
				copy(data[header.IPv4MinimumSize+header.UDPMinimumSize:], bytes.Repeat([]byte{byte(i + 1)}, 30))
				pkt := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: buffer.MakeWithData(data)})
				pkt.NetworkProtocolNumber = header.IPv4ProtocolNumber
				pkt.TransportProtocolNumber = header.UDPProtocolNumber
				pkt.NICID = 1
				if _, ok := pkt.NetworkHeader().Consume(header.IPv4MinimumSize); !ok {
					pkt.DecRef()
					b.Fatal("missing IPv4 header")
				}
				if _, ok := pkt.TransportHeader().Consume(header.UDPMinimumSize); !ok {
					pkt.DecRef()
					b.Fatal("missing UDP header")
				}
				queue.PushBack(&udpPacket{
					netProto:           header.IPv4ProtocolNumber,
					senderAddress:      tcpip.FullAddress{NIC: 1, Addr: remote, Port: 1234},
					destinationAddress: tcpip.FullAddress{NIC: 1, Addr: local, Port: 4321},
					packetInfo:         tcpip.IPPacketInfo{NIC: 1, LocalAddr: local, DestinationAddr: local},
					pkt:                pkt,
					receivedAt:         timestamp.Add(time.Duration(i)),
					tosOrTClass:        0x80,
					ttlOrHopLimit:      65,
				})
				memory += pkt.MemSize()
			}
			// These scenarios fit even the endpoint's initial 32KiB quota,
			// before newEndpoint applies any stack-specific override.
			if got, want := memory, 32*1024; got > want {
				b.Fatalf("queue memory estimate = %d, exceeds initial receive quota %d", got, want)
			}

			var encoded bytes.Buffer
			if _, err := save(b.Context(), &encoded, &queue); err != nil {
				b.Fatal(err)
			}
			wireSize := encoded.Len()
			// Keep the unchanged byte format as an actual graph check, outside
			// the measured loop. The queue contains no unordered maps.
			var ordinary bytes.Buffer
			if _, err := state.Save(b.Context(), &ordinary, &queue); err != nil {
				b.Fatal(err)
			}
			if got, want := encoded.Bytes(), ordinary.Bytes(); !bytes.Equal(got, want) {
				b.Fatalf("snapshot encoding = %x, want ordinary encoding %x", got, want)
			}
			var restored udpPacketList
			if _, err := state.Load(b.Context(), &encoded, &restored); err != nil {
				b.Fatal(err)
			}
			b.Cleanup(func() { releaseCodecQueue(&restored) })
			packet := restored.Front()
			var previous *udpPacket
			for i := range count {
				if packet == nil || packet.Prev() != previous {
					b.Fatalf("invalid restored queue link at packet %d", i)
				}
				if got, want := packet.receivedAt, timestamp.Add(time.Duration(i)); !got.Equal(want) {
					b.Fatalf("packet %d timestamp = %v, want %v", i, got, want)
				}
				view := packet.pkt.Data().AsRange().ToView()
				valid := bytes.Equal(view.AsSlice(), bytes.Repeat([]byte{byte(i + 1)}, 30))
				view.Release()
				if !valid {
					b.Fatalf("packet %d payload changed", i)
				}
				previous, packet = packet, packet.Next()
			}
			if packet != nil || restored.Back() != previous {
				b.Fatal("invalid restored queue tail")
			}
			// Release the one validation graph before measuring repeated Save.
			releaseCodecQueue(&restored)
			b.ReportAllocs()
			for b.Loop() {
				if _, err := save(b.Context(), io.Discard, &queue); err != nil {
					b.Fatal(err)
				}
			}
			b.ReportMetric(float64(wireSize), "wire-B/op")
			b.ReportMetric(float64(count), "packets/op")
		})
	}
}
