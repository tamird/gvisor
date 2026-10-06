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

package tcpip

import (
	"bytes"
	"testing"
)

func BenchmarkAddress(b *testing.B) {
	for _, tc := range []struct {
		name string
		data []byte
	}{
		{"IPv4", []byte{192, 0, 2, 1}},
		{"IPv6", []byte{0x20, 0x01, 0x0d, 0xb8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1}},
		{"MappedIPv4", []byte{0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff, 192, 0, 2, 1}},
	} {
		b.Run(tc.name, func(b *testing.B) {
			b.Run("Construct", func(b *testing.B) {
				b.ReportAllocs()
				var addr Address
				for b.Loop() {
					addr = AddrFromSlice(tc.data)
				}
				if !bytes.Equal(addr.AsSlice(), tc.data) {
					b.Fatalf("address = %v, want bytes %v", addr, tc.data)
				}
			})
			b.Run("CopyBytes", func(b *testing.B) {
				b.ReportAllocs()
				addr := AddrFromSlice(tc.data)
				var dst [16]byte
				for b.Loop() {
					copy(dst[:], addr.AsSlice())
				}
				if !bytes.Equal(dst[:len(tc.data)], tc.data) {
					b.Fatalf("copied bytes = %v, want %v", dst[:len(tc.data)], tc.data)
				}
			})
			b.Run("MapLookup", func(b *testing.B) {
				b.ReportAllocs()
				var addresses [256]Address
				lookup := make(map[Address]int, len(addresses))
				data := bytes.Clone(tc.data)
				for i := range addresses {
					data[len(data)-1] = byte(i)
					addresses[i] = AddrFromSlice(data)
					lookup[addresses[i]] = i
				}
				i := 0
				for b.Loop() {
					if got := lookup[addresses[i]]; got != i {
						b.Fatalf("lookup = %d, want %d", got, i)
					}
					i = (i + 1) % len(addresses)
				}
			})
		})
	}
}
