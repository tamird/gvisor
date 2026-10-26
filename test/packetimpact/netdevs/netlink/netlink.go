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

//go:build linux
// +build linux

// Package netlink has routines to get interfaces information through netlink.
package netlink

import (
	"fmt"
	"net/netip"

	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
)

// IfaceInfo returns the device with its IPv4 and IPv6 addresses. An error is
// returned if the device is not present or there are no or more than 1 ip addr
// per address family.
func IfaceInfo(name string) (netlink.Link, netip.Prefix, netip.Prefix, error) {
	link, err := netlink.LinkByName(name)
	if err != nil {
		return nil, netip.Prefix{}, netip.Prefix{}, fmt.Errorf("failed to get interface %s: %w", name, err)
	}
	ipv4Addrs, err := netlink.AddrList(link, unix.AF_INET)
	if err != nil {
		return nil, netip.Prefix{}, netip.Prefix{}, fmt.Errorf("failed to get ipv4 addrs: %w", err)
	}
	if len(ipv4Addrs) != 1 {
		return nil, netip.Prefix{}, netip.Prefix{}, fmt.Errorf("expected 1 ipv4 addresses, got %d", len(ipv4Addrs))
	}
	ipv6Addrs, err := netlink.AddrList(link, unix.AF_INET6)
	if err != nil {
		return nil, netip.Prefix{}, netip.Prefix{}, fmt.Errorf("failed to get ipv6 addrs: %w", err)
	}
	if len(ipv6Addrs) != 1 {
		return nil, netip.Prefix{}, netip.Prefix{}, fmt.Errorf("expected 1 ipv6 addresses, got %d", len(ipv6Addrs))
	}
	var prefixes [2]netip.Prefix
	for i, addr := range []netlink.Addr{ipv4Addrs[0], ipv6Addrs[0]} {
		if addr.IPNet == nil {
			return nil, netip.Prefix{}, netip.Prefix{}, fmt.Errorf("interface %s has an address without a prefix", name)
		}
		ip, ok := netip.AddrFromSlice(addr.IP)
		if !ok {
			return nil, netip.Prefix{}, netip.Prefix{}, fmt.Errorf("interface %s has invalid IP address %v", name, addr.IP)
		}
		ip = ip.Unmap()
		ones, bits := addr.Mask.Size()
		if ip.Is4() != (i == 0) || bits != ip.BitLen() {
			return nil, netip.Prefix{}, netip.Prefix{}, fmt.Errorf("interface %s has invalid address prefix %s", name, addr)
		}
		// Keep the interface address's host bits, not only its subnet.
		prefixes[i] = netip.PrefixFrom(ip, ones)
	}
	return link, prefixes[0], prefixes[1], nil
}
