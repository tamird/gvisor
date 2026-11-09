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

//go:build linux
// +build linux

// Fork-only control for an interrupted rtnetlink dump during PacketImpact bootstrap.
package netlink

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"

	rtnl "github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
)

func TestAddressDumps(t *testing.T) {
	for _, ns := range []string{"user", "net"} {
		child, err := os.Readlink("/proc/self/ns/" + ns)
		parent := os.Getenv("DUMP_PARENT_" + strings.ToUpper(ns))
		if err != nil || parent == "" || child == parent {
			t.Fatalf("expected a new %s namespace, parent=%q child=%q err=%v", ns, parent, child, err)
		}
	}
	links, err := rtnl.LinkList()
	if err != nil || len(links) != 1 || links[0].Attrs().Name != "lo" || os.Geteuid() != 0 {
		t.Fatalf("expected only loopback in private namespace, links=%v err=%v", links, err)
	}
	lo := links[0]
	if err := rtnl.LinkSetUp(lo); err != nil {
		t.Fatal(err)
	}

	// The initial reply deliberately carries the correct single address, as a
	// partial dump can. Only a second complete kernel lookup can satisfy us.
	for _, family := range []int{unix.AF_INET, unix.AF_INET6} {
		t.Run(fmt.Sprintf("first_interruption_%d", family), func(t *testing.T) {
			calls := 0
			injectedReply := false
			queryAddrs = func(link rtnl.Link, af int) ([]rtnl.Addr, error) {
				addrs, err := rtnl.AddrList(link, af)
				if af == family {
					calls++
					if err == nil && calls == 1 {
						if len(addrs) != 1 {
							t.Fatalf("first kernel reply has %d addresses", len(addrs))
						}
						injectedReply = true
						return addrs, fmt.Errorf("injected after kernel dump: %w", rtnl.ErrDumpInterrupted)
					}
				}
				return addrs, err
			}
			t.Cleanup(func() { queryAddrs = rtnl.AddrList })
			_, v4, v6, err := IfaceInfo("lo")
			if err != nil || !injectedReply || calls != 2 || v4.IP.String() != "127.0.0.1" || v6.IP.String() != "::1" {
				t.Fatalf("family=%d attempts=%d ipv4=%v ipv6=%v err=%v", family, calls, v4.IPNet, v6.IPNet, err)
			}
			t.Logf("complete kernel lookup family=%d attempts=%d ipv4=%s ipv6=%s", family, calls, v4.IP, v6.IP)
		})
	}
	for _, tc := range []struct {
		name      string
		cause     error
		wantCalls int
	}{
		{"persistent_interruption", rtnl.ErrDumpInterrupted, 10},
		{"permission", unix.EACCES, 1},
		{"bare_EINTR", unix.EINTR, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			injected := fmt.Errorf("injected after kernel dump: %w", tc.cause)
			queryAddrs = func(link rtnl.Link, af int) ([]rtnl.Addr, error) {
				addrs, err := rtnl.AddrList(link, af)
				if af == unix.AF_INET6 && err == nil {
					calls++
					return addrs, injected
				}
				return addrs, err
			}
			t.Cleanup(func() { queryAddrs = rtnl.AddrList })
			link, v4, v6, err := IfaceInfo("lo")
			if calls != tc.wantCalls || !errors.Is(err, injected) || !strings.HasPrefix(err.Error(), "failed to get ipv6 addrs: ") || link != nil || v4.IPNet != nil || v6.IPNet != nil {
				t.Fatalf("attempts=%d ipv4=%v ipv6=%v err=%v", calls, v4.IPNet, v6.IPNet, err)
			}
			t.Logf("attempts=%d returned_no_addresses=true err=%v", calls, err)
		})
	}

	veth := &rtnl.Veth{LinkAttrs: rtnl.LinkAttrs{Name: "dump-target"}, PeerName: "dump-peer"}
	if err := rtnl.LinkAdd(veth); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := rtnl.LinkDel(veth); err != nil {
			t.Error(err)
		}
	})
	target, err := rtnl.LinkByName("dump-target")
	if err != nil {
		t.Fatal(err)
	}
	address := func(spec string) *rtnl.Addr {
		a, err := rtnl.ParseAddr(spec)
		if err != nil {
			t.Fatal(err)
		}
		return a
	}
	t.Run("completed_cardinality", func(t *testing.T) {
		for _, tc := range []struct{ add, want string }{
			{"", "expected 1 ipv4 addresses, got 0"},
			{"192.0.2.1/32", "expected 1 ipv6 addresses, got 0"},
			{"192.0.2.2/32", "expected 1 ipv4 addresses, got 2"},
		} {
			if tc.add != "" {
				if err := rtnl.AddrAdd(target, address(tc.add)); err != nil {
					t.Fatal(err)
				}
			}
			_, _, _, err := IfaceInfo("dump-target")
			if err == nil || err.Error() != tc.want {
				t.Fatalf("after %q: got %v, want %q", tc.add, err, tc.want)
			}
		}
		extra := address("2001:db8::2/128")
		if err := rtnl.AddrAdd(lo, extra); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			if err := rtnl.AddrDel(lo, extra); err != nil {
				t.Error(err)
			}
		})
		_, _, _, err := IfaceInfo("lo")
		if err == nil || err.Error() != "expected 1 ipv6 addresses, got 2" {
			t.Fatalf("second IPv6 address: %v", err)
		}
	})
	t.Run("sibling_address_changes", func(t *testing.T) {
		// The kernel chooses whether to interrupt any given dump. The counter is
		// observational; the controlled cases above test the retry deterministically.
		intrs := 0
		queryAddrs = func(link rtnl.Link, af int) ([]rtnl.Addr, error) {
			addrs, err := rtnl.AddrList(link, af)
			if errors.Is(err, rtnl.ErrDumpInterrupted) {
				intrs++
			}
			return addrs, err
		}
		t.Cleanup(func() { queryAddrs = rtnl.AddrList })
		extra := address("2001:db8::99/128")
		for range 16 {
			done := make(chan error, 1)
			go func() {
				err := rtnl.AddrAdd(target, extra)
				if err == nil {
					err = rtnl.AddrDel(target, extra)
				}
				done <- err
			}()
			_, v4, v6, err := IfaceInfo("lo")
			if changeErr := <-done; changeErr != nil {
				t.Fatal(changeErr)
			}
			if err != nil || v4.IP.String() != "127.0.0.1" || v6.IP.String() != "::1" {
				t.Fatalf("lookup during address change: %v, %v, %v", v4.IPNet, v6.IPNet, err)
			}
		}
		t.Logf("lookups=16 sibling_add_delete_pairs=16 actual_kernel_interruptions=%d", intrs)
	})
}
