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
	"errors"
	"maps"
	"net/netip"
	"slices"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/state"
)

func init() {
	state.Register((*time.Time)(nil))
	state.Register((*binaryFailure)(nil))
}

func TestBinaryValues(t *testing.T) {
	values := []any{time.Time{}, time.Date(2026, time.October, 7, 12, 0, 0, 1, time.UTC), inner{42}}
	runTestCases(t, false, "values", values)
	runTestCases(t, false, "interfaces", interfacesTo(values))
}

var errBinary = errors.New("binary codec failed")

// Existing state methods must take precedence over binary methods.
func (*inner) MarshalBinary() ([]byte, error) { return nil, errBinary }

func (*inner) UnmarshalBinary([]byte) error { return errBinary }

type binaryFailure struct {
	failSave bool
}

func (b *binaryFailure) MarshalBinary() ([]byte, error) {
	if b.failSave {
		return nil, errBinary
	}
	return nil, nil
}

func (*binaryFailure) UnmarshalBinary([]byte) error { return errBinary }

func TestBinaryErrors(t *testing.T) {
	var buf bytes.Buffer
	if _, err := state.Save(t.Context(), &buf, &binaryFailure{failSave: true}); !errors.Is(err, errBinary) {
		t.Fatalf("Save = %v, want %v", err, errBinary)
	}
	buf.Reset()
	if _, err := state.Save(t.Context(), &buf, &binaryFailure{}); err != nil {
		t.Fatalf("Save: %v", err)
	}
	if _, err := state.Load(t.Context(), &buf, &binaryFailure{}); !errors.Is(err, errBinary) {
		t.Fatalf("Load = %v, want %v", err, errBinary)
	}
}

func TestNetipValues(t *testing.T) {
	addresses := []netip.Addr{
		{},
		netip.MustParseAddr("0.0.0.0"),
		netip.MustParseAddr("192.0.2.1"),
		netip.MustParseAddr("::ffff:192.0.2.1"),
		netip.MustParseAddr("::"),
		netip.MustParseAddr("2001:db8::1"),
		netip.MustParseAddr("fe80::1%eth0"),
	}
	prefixes := []netip.Prefix{
		{},
		netip.PrefixFrom(netip.MustParseAddr("192.0.2.1"), 33),
		netip.MustParsePrefix("0.0.0.0/0"),
		netip.MustParsePrefix("192.0.2.1/24"),
		netip.MustParsePrefix("192.0.2.1/32"),
		netip.MustParsePrefix("::/0"),
		netip.MustParsePrefix("::ffff:192.0.2.1/120"),
		netip.MustParsePrefix("2001:db8::1/64"),
		netip.MustParsePrefix("2001:db8::1/128"),
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
		prefix:        netip.MustParsePrefix("2001:db8::1/64"),
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
	if restored.address != original.address || restored.prefix != original.prefix || restored.addressPort != original.addressPort {
		t.Errorf("fields = (%v, %v, %v), want (%v, %v, %v)", restored.address, restored.prefix, restored.addressPort, original.address, original.prefix, original.addressPort)
	}
	if restored.addressPointer != &restored.address {
		t.Errorf("addressPointer = %p, want alias %p", restored.addressPointer, &restored.address)
	}
	if restored.prefixPointer != &restored.prefix {
		t.Errorf("prefixPointer = %p, want alias %p", restored.prefixPointer, &restored.prefix)
	}
	if restored.addrPortPointer != &restored.addressPort {
		t.Errorf("addrPortPointer = %p, want alias %p", restored.addrPortPointer, &restored.addressPort)
	}
	if !slices.Equal(restored.addresses, addresses) {
		t.Errorf("addresses = %v, want %v", restored.addresses, addresses)
	}
	if !slices.Equal(restored.prefixes, prefixes) {
		t.Errorf("prefixes = %v, want %v", restored.prefixes, prefixes)
	}
	if !slices.Equal(restored.addressPorts, addressPorts) {
		t.Errorf("addressPorts = %v, want %v", restored.addressPorts, addressPorts)
	}
	if !maps.Equal(restored.byAddress, original.byAddress) {
		t.Errorf("byAddress = %v, want %v", restored.byAddress, original.byAddress)
	}
	if !maps.Equal(restored.byPrefix, original.byPrefix) {
		t.Errorf("byPrefix = %v, want %v", restored.byPrefix, original.byPrefix)
	}
	if !maps.Equal(restored.byAddressPort, original.byAddressPort) {
		t.Errorf("byAddressPort = %v, want %v", restored.byAddressPort, original.byAddressPort)
	}
	if !slices.Equal(restored.values, original.values) {
		t.Errorf("interfaces = %v, want %v", restored.values, original.values)
	}
}
