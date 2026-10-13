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

func TestNetipValues(t *testing.T) {
	addresses := []netip.Addr{
		{},
		netip.IPv4Unspecified(),
		netip.MustParseAddr("192.0.2.1"),
		netip.MustParseAddr("::ffff:192.0.2.1"),
		netip.IPv6Unspecified(),
		netip.MustParseAddr("2001:db8::1"),
		netip.MustParseAddr("fe80::1%eth0"),
	}
	prefixes := []netip.Prefix{
		{},
		netip.PrefixFrom(netip.MustParseAddr("192.0.2.1"), 33),
		netip.PrefixFrom(netip.IPv4Unspecified(), 0),
		netip.PrefixFrom(netip.MustParseAddr("192.0.2.1"), 24),
		netip.PrefixFrom(netip.MustParseAddr("192.0.2.1"), 32),
		netip.PrefixFrom(netip.IPv6Unspecified(), 0),
		netip.PrefixFrom(netip.MustParseAddr("::ffff:192.0.2.1"), 120),
		netip.PrefixFrom(netip.MustParseAddr("2001:db8::1"), 64),
		netip.PrefixFrom(netip.MustParseAddr("2001:db8::1"), 128),
	}
	addressPorts := []netip.AddrPort{
		{},
		netip.AddrPortFrom(netip.Addr{}, 80),
		netip.MustParseAddrPort("0.0.0.0:80"),
		netip.MustParseAddrPort("192.0.2.1:65535"),
		netip.MustParseAddrPort("[::ffff:192.0.2.1]:80"),
		netip.MustParseAddrPort("[::]:0"),
		netip.MustParseAddrPort("[2001:db8::1]:80"),
		netip.MustParseAddrPort("[fe80::1%eth0]:80"),
	}
	original := netipContainer{
		address:       netip.MustParseAddr("fe80::1%eth0"),
		prefix:        netip.PrefixFrom(netip.MustParseAddr("2001:db8::1"), 64),
		addressPort:   netip.MustParseAddrPort("[fe80::1%eth0]:80"),
		addresses:     addresses,
		prefixes:      prefixes,
		addressPorts:  addressPorts,
		byAddress:     make(map[netip.Addr]int, len(addresses)),
		byPrefix:      make(map[netip.Prefix]int, len(prefixes)),
		byAddressPort: make(map[netip.AddrPort]int, len(addressPorts)),
	}
	original.addressPointer = &original.address
	original.prefixPointer = &original.prefix
	original.addrPortPointer = &original.addressPort
	for i, a := range addresses {
		original.byAddress[a] = i
		original.values = append(original.values, a)
	}
	for i, p := range prefixes {
		original.byPrefix[p] = i
		original.values = append(original.values, p)
	}
	for i, ap := range addressPorts {
		original.byAddressPort[ap] = i
		original.values = append(original.values, ap)
	}
	var buf bytes.Buffer
	if _, err := state.Save(t.Context(), &buf, &original); err != nil {
		t.Fatalf("Save: %v", err)
	}
	var restored netipContainer
	if _, err := state.Load(t.Context(), &buf, &restored); err != nil {
		t.Fatalf("Load: %v", err)
	}
	// Use equality and map lookups, not reflect.DeepEqual: distinct interned
	// handles can contain deeply equal contents but remain different map keys.
	if got, want := restored.address, original.address; got != want {
		t.Errorf("address = %v, want %v", got, want)
	}
	if got, want := restored.prefix, original.prefix; got != want {
		t.Errorf("prefix = %v, want %v", got, want)
	}
	if got, want := restored.addressPort, original.addressPort; got != want {
		t.Errorf("addressPort = %v, want %v", got, want)
	}
	if got, want := restored.addressPointer, &restored.address; got != want {
		t.Errorf("addressPointer = %p, want %p", got, want)
	}
	if got, want := restored.prefixPointer, &restored.prefix; got != want {
		t.Errorf("prefixPointer = %p, want %p", got, want)
	}
	if got, want := restored.addrPortPointer, &restored.addressPort; got != want {
		t.Errorf("addrPortPointer = %p, want %p", got, want)
	}
	if got, want := restored.addresses, addresses; !slices.Equal(got, want) {
		t.Errorf("addresses = %v, want %v", got, want)
	}
	if got, want := restored.prefixes, prefixes; !slices.Equal(got, want) {
		t.Errorf("prefixes = %v, want %v", got, want)
	}
	if got, want := restored.addressPorts, addressPorts; !slices.Equal(got, want) {
		t.Errorf("addressPorts = %v, want %v", got, want)
	}
	if got, want := restored.byAddress, original.byAddress; !maps.Equal(got, want) {
		t.Errorf("byAddress = %v, want %v", got, want)
	}
	if got, want := restored.byPrefix, original.byPrefix; !maps.Equal(got, want) {
		t.Errorf("byPrefix = %v, want %v", got, want)
	}
	if got, want := restored.byAddressPort, original.byAddressPort; !maps.Equal(got, want) {
		t.Errorf("byAddressPort = %v, want %v", got, want)
	}
	if got, want := restored.values, original.values; !slices.Equal(got, want) {
		t.Errorf("interfaces = %v, want %v", got, want)
	}
}
