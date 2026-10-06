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

package tests

import (
	"bytes"
	"maps"
	"net/netip"
	"slices"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
)

func TestNetipAddresses(t *testing.T) {
	addresses := []netip.Addr{
		{},
		netip.MustParseAddr("0.0.0.0"),
		netip.MustParseAddr("192.0.2.1"),
		netip.MustParseAddr("::ffff:192.0.2.1"),
		netip.MustParseAddr("::"),
		netip.MustParseAddr("2001:db8::1"),
		netip.MustParseAddr("fe80::1%eth0"),
	}
	for _, addr := range addresses {
		t.Run(addr.String(), func(t *testing.T) {
			original := netipContainer{
				address:   addr,
				addresses: addresses,
				byAddress: make(map[netip.Addr]int, len(addresses)),
				value:     addr,
			}
			original.pointer = &original.address
			for i, a := range addresses {
				original.byAddress[a] = i
			}
			var buf bytes.Buffer
			if _, err := state.Save(t.Context(), &buf, &original); err != nil {
				t.Fatalf("Save: %v", err)
			}
			var restored netipContainer
			if _, err := state.Load(t.Context(), &buf, &restored); err != nil {
				t.Fatalf("Load: %v", err)
			}
			// Use address equality and map lookups, not reflect.DeepEqual:
			// distinct interned handles can contain deeply equal contents.
			if restored.address != addr {
				t.Errorf("address = %v, want %v", restored.address, addr)
			}
			if restored.pointer != &restored.address {
				t.Errorf("pointer = %p, want alias %p", restored.pointer, &restored.address)
			}
			if !slices.Equal(restored.addresses, addresses) {
				t.Errorf("addresses = %v, want %v", restored.addresses, addresses)
			}
			if !maps.Equal(restored.byAddress, original.byAddress) {
				t.Errorf("byAddress = %v, want %v", restored.byAddress, original.byAddress)
			}
			if restored.value != addr {
				t.Errorf("interface = %T(%v), want netip.Addr(%v)", restored.value, restored.value, addr)
			}
		})
	}
}
