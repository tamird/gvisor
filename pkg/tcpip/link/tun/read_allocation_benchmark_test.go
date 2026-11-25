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

package tun

import (
	"bytes"
	"testing"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

// BenchmarkReadSegmented measures outgoing segmented packet ownership through
// Device.Read. It excludes user-memory copies and the guest/network round trip.
func BenchmarkReadSegmented(b *testing.B) {
	b.StopTimer()
	const size = 1500
	link := channel.New(1, size, "")
	defer link.Close()
	d := Device{endpoint: &tunEndpoint{Endpoint: link}, flags: Flags{TUN: true, NoPacketInfo: true}}
	var payload, check [size]byte
	for i := range payload {
		payload[i] = byte(i*17 + 5)
	}
	payload[0] = 0x45
	b.ReportAllocs()
	for i := -1; i < b.N; i++ {
		var input buffer.Buffer
		for offset := 0; offset < size; offset += 500 {
			part := buffer.MakeWithData(payload[offset : offset+500])
			input.Merge(&part)
		}
		pkt := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: input})
		pkt.NetworkProtocolNumber = header.IPv4ProtocolNumber
		var packets stack.PacketBufferList
		packets.PushBack(pkt)
		n, err := link.WritePackets(packets)
		pkt.DecRef()
		if err != nil || n != 1 {
			b.Fatalf("WritePackets = (%d, %v), want (1, nil)", n, err)
		}
		data, err := d.Read()
		if err != nil {
			b.Fatalf("Read: %v", err)
		}
		if got := data.Size(); got != size {
			data.Release()
			b.Fatalf("Read size = %d, want %d", got, size)
		}
		if i == -1 {
			n, err := data.ReadAt(check[:], 0)
			data.Release()
			if err != nil || n != size || !bytes.Equal(check[:], payload[:]) {
				b.Fatalf("Read content = (%d, %v, %x), want %x", n, err, check, payload)
			}
			b.ResetTimer()
			b.StartTimer()
			continue
		}
		data.Release()
	}
}
