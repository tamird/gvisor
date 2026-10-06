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

package sandbox

import (
	"bytes"
	"errors"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"testing"
	"time"

	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/runsc/boot"
	"gvisor.dev/gvisor/runsc/config"
)

func init() {
	// Reserve the initial thread for main so namespace tests use threads that
	// can exit if namespace restoration fails. Locking during init pins main
	// to this thread: https://pkg.go.dev/runtime#LockOSThread.
	runtime.LockOSThread()
}

func fdbasedLinkEqual(a, b boot.FDBasedLink) bool {
	if a.Name != b.Name {
		return false
	}
	if a.MTU != b.MTU {
		return false
	}
	if a.QDisc != b.QDisc {
		return false
	}
	if !bytes.Equal(a.LinkAddress, b.LinkAddress) {
		return false
	}
	if !slices.Equal(a.Addresses, b.Addresses) {
		return false
	}
	if !slices.Equal(a.Routes, b.Routes) {
		return false
	}
	if len(a.Neighbors) != len(b.Neighbors) {
		return false
	}
	for i := range a.Neighbors {
		if a.Neighbors[i].IP != b.Neighbors[i].IP {
			return false
		}
		if !bytes.Equal(a.Neighbors[i].HardwareAddr, b.Neighbors[i].HardwareAddr) {
			return false
		}
	}
	return true
}

func fdbasedLinksEqual(a, b []boot.FDBasedLink) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if !fdbasedLinkEqual(a[i], b[i]) {
			return false
		}
	}
	return true
}

func loopbackLinksEqual(a, b []boot.LoopbackLink) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i].Name != b[i].Name {
			return false
		}
		if !slices.Equal(a[i].Addresses, b[i].Addresses) {
			return false
		}
		if !slices.Equal(a[i].Routes, b[i].Routes) {
			return false
		}
	}
	return true
}

func requireRoot(t *testing.T) {
	t.Helper()
	if os.Getuid() != 0 {
		t.Skip("Skipping integration test: must run as root")
	}
}

func setupTestNamespace(t *testing.T) {
	t.Helper()

	// Network namespaces are per-thread. Pin before saving or changing one.
	runtime.LockOSThread()
	restoreFailed := false
	t.Cleanup(func() {
		if !restoreFailed {
			runtime.UnlockOSThread()
		}
	})
	origNs, err := unix.Open("/proc/thread-self/ns/net", unix.O_RDONLY, 0)
	if err != nil {
		t.Fatalf("Failed to get current netns: %v", err)
	}

	t.Cleanup(func() { unix.Close(origNs) })

	if err := unix.Unshare(unix.CLONE_NEWNET); err != nil {
		t.Fatalf("Failed to unshare netns: %v", err)
	}

	t.Cleanup(func() {
		if err := unix.Setns(origNs, unix.CLONE_NEWNET); err != nil {
			// Let the test goroutine exit with the thread locked rather than
			// returning a thread in the wrong namespace to the runtime.
			restoreFailed = true
			t.Errorf("Failed to restore original netns: %v", err)
		}
	})
}

func createVethPair(t *testing.T, name string) netlink.Link {
	t.Helper()

	veth := &netlink.Veth{
		LinkAttrs: netlink.LinkAttrs{
			Name: name,
		},
		PeerName: name + "-peer",
	}

	if err := netlink.LinkAdd(veth); err != nil {
		t.Fatalf("Failed to create veth pair: %v", err)
	}
	t.Cleanup(func() {
		if err := netlink.LinkDel(veth); err != nil {
			t.Errorf("Failed to delete veth pair: %v", err)
		}
	})

	link, err := netlink.LinkByName(name)
	if err != nil {
		t.Fatalf("Failed to get veth link: %v", err)
	}
	return link
}

func setupVethInterface(t *testing.T, name, ip string, prefixLen, addrBits int) netlink.Link {
	t.Helper()

	setupTestNamespace(t)
	link := createVethPair(t, name)

	if err := netlink.LinkSetUp(link); err != nil {
		t.Fatalf("Failed to bring up interface: %v", err)
	}

	addr := &netlink.Addr{
		IPNet: &net.IPNet{
			IP:   net.ParseIP(ip),
			Mask: net.CIDRMask(prefixLen, addrBits),
		},
	}
	if err := netlink.AddrAdd(link, addr); err != nil {
		t.Fatalf("Failed to add address: %v", err)
	}

	return link
}

func addRoute(t *testing.T, dst *net.IPNet, gw net.IP) {
	t.Helper()
	route := &netlink.Route{Dst: dst, Gw: gw}
	if err := netlink.RouteAdd(route); err != nil {
		t.Fatalf("Failed to add route (dst=%v, gw=%v): %v", dst, gw, err)
	}
}

func addNeighbor(t *testing.T, link netlink.Link, ip net.IP, hwAddr string) {
	t.Helper()
	hw, err := net.ParseMAC(hwAddr)
	if err != nil {
		t.Fatalf("Failed to parse MAC %s: %v", hwAddr, err)
	}
	neigh := &netlink.Neigh{
		LinkIndex:    link.Attrs().Index,
		IP:           ip,
		HardwareAddr: hw,
		State:        netlink.NUD_PERMANENT,
	}
	if err := netlink.NeighAdd(neigh); err != nil {
		t.Fatalf("Failed to add neighbor: %v", err)
	}
}

func parseCIDR(t *testing.T, cidr string) *net.IPNet {
	t.Helper()
	_, ipNet, err := net.ParseCIDR(cidr)
	if err != nil {
		t.Fatalf("Failed to parse CIDR %s: %v", cidr, err)
	}
	return ipNet
}

func setupLoopback(t *testing.T) {
	t.Helper()
	loLink, err := netlink.LinkByName("lo")
	if err != nil {
		t.Fatalf("Failed to get lo link: %v", err)
	}
	if err := netlink.LinkSetUp(loLink); err != nil {
		t.Fatalf("Failed to bring up lo: %v", err)
	}
	err = netlink.AddrAdd(loLink, &netlink.Addr{
		IPNet: &net.IPNet{IP: net.ParseIP("127.0.0.1"), Mask: net.CIDRMask(8, 32)},
	})
	if err != nil && err != unix.EEXIST {
		t.Fatalf("Failed to add address to lo: %v", err)
	}
}

// defaultLoopbackLinks returns the expected loopback links for a standard
// loopback interface with 127.0.0.1/8 and ::1/128.
func defaultLoopbackLinks() []boot.LoopbackLink {
	return []boot.LoopbackLink{
		{
			Name: "lo",
			Addresses: []netip.Prefix{
				netip.MustParsePrefix("127.0.0.1/8"),
				netip.MustParsePrefix("::1/128"),
			},
			Routes: []boot.Route{
				{
					Destination: netip.MustParsePrefix("127.0.0.0/8"),
				},
				{
					Destination: netip.MustParsePrefix("::1/128"),
				},
			},
		},
	}
}

func TestCollectLinksAndRoutes_SingleInterface(t *testing.T) {
	requireRoot(t)
	link := setupVethInterface(t, "testveth0", "10.0.0.1", 24, 32)
	setupLoopback(t)
	addRoute(t, nil, net.ParseIP("10.0.0.254"))
	addRoute(t, parseCIDR(t, "192.168.1.0/24"), net.ParseIP("10.0.0.2"))
	addNeighbor(t, link, net.ParseIP("10.0.0.2"), "00:11:22:33:44:55")

	conf := &config.Config{
		XDP: config.XDP{Mode: config.XDPModeOff},
	}

	args, err := collectLinksAndRoutes(conf, false)
	if err != nil {
		t.Fatalf("collectLinksAndRoutes failed: %v", err)
	}

	wantFDLinks := []boot.FDBasedLink{
		{
			Name:        "testveth0",
			MTU:         1500,
			LinkAddress: link.Attrs().HardwareAddr,
			QDisc:       config.QDiscNone,
			Addresses: []netip.Prefix{
				netip.MustParsePrefix("10.0.0.1/24"),
			},
			Routes: []boot.Route{
				{
					Destination: netip.MustParsePrefix("10.0.0.0/24"),
				},
				{
					Destination: netip.MustParsePrefix("192.168.1.0/24"),
					Gateway:     netip.MustParseAddr("10.0.0.2"),
				},
			},
			Neighbors: []boot.Neighbor{
				{IP: netip.MustParseAddr("10.0.0.2"), HardwareAddr: mustParseMAC("00:11:22:33:44:55")},
			},
		},
	}

	if !fdbasedLinksEqual(args.FDBasedLinks, wantFDLinks) {
		t.Errorf("FDBasedLinks mismatch:\ngot  %+v\nwant %+v", args.FDBasedLinks, wantFDLinks)
	}

	wantGW := boot.DefaultRoute{
		Name: "testveth0",
		Route: boot.Route{
			Destination: netip.MustParsePrefix("0.0.0.0/0"),
			Gateway:     netip.MustParseAddr("10.0.0.254"),
		},
	}
	if args.Defaultv4Gateway != wantGW {
		t.Errorf("Defaultv4Gateway mismatch:\ngot  %+v\nwant %+v", args.Defaultv4Gateway, wantGW)
	}

	if !loopbackLinksEqual(args.LoopbackLinks, defaultLoopbackLinks()) {
		t.Errorf("LoopbackLinks mismatch:\ngot  %+v\nwant %+v", args.LoopbackLinks, defaultLoopbackLinks())
	}
}

func mustParseMAC(s string) net.HardwareAddr {
	hw, err := net.ParseMAC(s)
	if err != nil {
		panic(err)
	}
	return hw
}

func TestCollectLinksAndRoutes_LoopbackOnly(t *testing.T) {
	requireRoot(t)
	setupTestNamespace(t)
	setupLoopback(t)

	conf := &config.Config{
		XDP: config.XDP{Mode: config.XDPModeOff},
	}

	args, err := collectLinksAndRoutes(conf, false)
	if err != nil {
		t.Fatalf("collectLinksAndRoutes failed: %v", err)
	}

	wantLoopbackLinks := []boot.LoopbackLink{
		{
			Name: "lo",
			Addresses: []netip.Prefix{
				netip.MustParsePrefix("127.0.0.1/8"),
				netip.MustParsePrefix("::1/128"),
			},
			Routes: []boot.Route{
				{
					Destination: netip.MustParsePrefix("127.0.0.0/8"),
				},
				{
					Destination: netip.MustParsePrefix("::1/128"),
				},
			},
		},
	}
	if !loopbackLinksEqual(args.LoopbackLinks, wantLoopbackLinks) {
		t.Errorf("LoopbackLinks mismatch:\ngot  %+v\nwant %+v", args.LoopbackLinks, wantLoopbackLinks)
	}

	if !fdbasedLinksEqual(args.FDBasedLinks, nil) {
		t.Errorf("FDBasedLinks mismatch:\ngot  %+v\nwant nil", args.FDBasedLinks)
	}
}

func TestCollectLinksAndRoutes_MultipleInterfaces(t *testing.T) {
	requireRoot(t)
	setupTestNamespace(t)
	setupLoopback(t)

	veth0Link := createVethPair(t, "testveth0")
	veth1Link := createVethPair(t, "testveth1")
	if err := netlink.LinkSetUp(veth0Link); err != nil {
		t.Fatalf("Failed to bring up interface: %v", err)
	}
	if err := netlink.LinkSetUp(veth1Link); err != nil {
		t.Fatalf("Failed to bring up interface: %v", err)
	}
	if err := netlink.AddrAdd(veth0Link, &netlink.Addr{
		IPNet: &net.IPNet{IP: net.ParseIP("10.0.0.1"), Mask: net.CIDRMask(24, 32)},
	}); err != nil {
		t.Fatalf("Failed to add address: %v", err)
	}
	if err := netlink.AddrAdd(veth1Link, &netlink.Addr{
		IPNet: &net.IPNet{IP: net.ParseIP("192.168.1.1"), Mask: net.CIDRMask(24, 32)},
	}); err != nil {
		t.Fatalf("Failed to add address: %v", err)
	}

	addRoute(t, nil, net.ParseIP("10.0.0.254"))

	conf := &config.Config{
		XDP: config.XDP{Mode: config.XDPModeOff},
	}

	args, err := collectLinksAndRoutes(conf, false)
	if err != nil {
		t.Fatalf("collectLinksAndRoutes failed: %v", err)
	}

	wantFDLinks := []boot.FDBasedLink{
		{
			Name:        "testveth0",
			MTU:         1500,
			LinkAddress: veth0Link.Attrs().HardwareAddr,
			QDisc:       config.QDiscNone,
			Addresses: []netip.Prefix{
				netip.MustParsePrefix("10.0.0.1/24"),
			},
			Routes: []boot.Route{
				{
					Destination: netip.MustParsePrefix("10.0.0.0/24"),
				},
			},
		},
		{
			Name:        "testveth1",
			MTU:         1500,
			LinkAddress: veth1Link.Attrs().HardwareAddr,
			QDisc:       config.QDiscNone,
			Addresses: []netip.Prefix{
				netip.MustParsePrefix("192.168.1.1/24"),
			},
			Routes: []boot.Route{
				{
					Destination: netip.MustParsePrefix("192.168.1.0/24"),
				},
			},
		},
	}
	if !fdbasedLinksEqual(args.FDBasedLinks, wantFDLinks) {
		t.Errorf("FDBasedLinks mismatch:\ngot  %+v\nwant %+v", args.FDBasedLinks, wantFDLinks)
	}

	wantGW := boot.DefaultRoute{
		Name: "testveth0",
		Route: boot.Route{
			Destination: netip.MustParsePrefix("0.0.0.0/0"),
			Gateway:     netip.MustParseAddr("10.0.0.254"),
		},
	}
	if args.Defaultv4Gateway != wantGW {
		t.Errorf("Defaultv4Gateway mismatch:\ngot  %+v\nwant %+v", args.Defaultv4Gateway, wantGW)
	}

	if !loopbackLinksEqual(args.LoopbackLinks, defaultLoopbackLinks()) {
		t.Errorf("LoopbackLinks mismatch:\ngot  %+v\nwant %+v", args.LoopbackLinks, defaultLoopbackLinks())
	}
}

func TestCollectLinksAndRoutes_IPv6Disabled(t *testing.T) {
	requireRoot(t)
	setupTestNamespace(t)
	setupLoopback(t)

	veth0Link := createVethPair(t, "testveth0")
	if err := netlink.LinkSetUp(veth0Link); err != nil {
		t.Fatalf("Failed to bring up interface: %v", err)
	}
	if err := netlink.AddrAdd(veth0Link, &netlink.Addr{
		IPNet: &net.IPNet{IP: net.ParseIP("10.0.0.1"), Mask: net.CIDRMask(24, 32)},
	}); err != nil {
		t.Fatalf("Failed to add address: %v", err)
	}
	if err := netlink.AddrAdd(veth0Link, &netlink.Addr{
		IPNet: &net.IPNet{IP: net.ParseIP("2001:db8::1"), Mask: net.CIDRMask(64, 128)},
	}); err != nil {
		t.Fatalf("Failed to add address: %v", err)
	}

	addRoute(t, nil, net.ParseIP("10.0.0.254"))
	addRoute(t, nil, net.ParseIP("2001:db8::ffff"))

	conf := &config.Config{
		XDP: config.XDP{Mode: config.XDPModeOff},
	}

	args, err := collectLinksAndRoutes(conf, true)
	if err != nil {
		t.Fatalf("collectLinksAndRoutes failed: %v", err)
	}

	wantFDLinks := []boot.FDBasedLink{
		{
			Name:        "testveth0",
			MTU:         1500,
			LinkAddress: veth0Link.Attrs().HardwareAddr,
			QDisc:       config.QDiscNone,
			Addresses: []netip.Prefix{
				netip.MustParsePrefix("10.0.0.1/24"),
			},
			Routes: []boot.Route{
				{
					Destination: netip.MustParsePrefix("10.0.0.0/24"),
				},
			},
		},
	}

	if !fdbasedLinksEqual(args.FDBasedLinks, wantFDLinks) {
		t.Errorf("FDBasedLinks mismatch:\ngot  %+v\nwant %+v", args.FDBasedLinks, wantFDLinks)
	}

	if !args.Defaultv6Gateway.Route.Empty() {
		t.Errorf("Defaultv6Gateway.Route should be empty, got %+v", args.Defaultv6Gateway.Route)
	}

	wantLoopbackLinks := []boot.LoopbackLink{
		{
			Name: "lo",
			Addresses: []netip.Prefix{
				netip.MustParsePrefix("127.0.0.1/8"),
			},
			Routes: []boot.Route{
				{
					Destination: netip.MustParsePrefix("127.0.0.0/8"),
				},
			},
		},
	}
	if !loopbackLinksEqual(args.LoopbackLinks, wantLoopbackLinks) {
		t.Errorf("LoopbackLinks mismatch:\ngot  %+v\nwant %+v", args.LoopbackLinks, wantLoopbackLinks)
	}
}

func TestCollectLinksAndRoutes_DownInterface(t *testing.T) {
	requireRoot(t)
	setupTestNamespace(t)

	createVethPair(t, "testveth0")

	conf := &config.Config{
		XDP: config.XDP{Mode: config.XDPModeOff},
	}

	args, err := collectLinksAndRoutes(conf, false)
	if err != nil {
		t.Fatalf("collectLinksAndRoutes failed: %v", err)
	}

	if !fdbasedLinksEqual(args.FDBasedLinks, nil) {
		t.Errorf("FDBasedLinks mismatch:\ngot  %+v\nwant nil", args.FDBasedLinks)
	}

	if !loopbackLinksEqual(args.LoopbackLinks, nil) {
		t.Errorf("LoopbackLinks mismatch:\ngot  %+v\nwant nil", args.LoopbackLinks)
	}
}

func TestCollectLinksAndRoutes_NoUsableAddresses(t *testing.T) {
	requireRoot(t)
	setupTestNamespace(t)

	veth0Link := createVethPair(t, "testveth0")
	if err := netlink.LinkSetUp(veth0Link); err != nil {
		t.Fatalf("Failed to bring up interface: %v", err)
	}
	if err := netlink.AddrAdd(veth0Link, &netlink.Addr{
		IPNet: &net.IPNet{IP: net.ParseIP("2001:db8::1"), Mask: net.CIDRMask(64, 128)},
	}); err != nil {
		t.Fatalf("Failed to add address: %v", err)
	}

	conf := &config.Config{
		XDP: config.XDP{Mode: config.XDPModeOff},
	}

	args, err := collectLinksAndRoutes(conf, true)
	if err != nil {
		t.Fatalf("collectLinksAndRoutes failed: %v", err)
	}

	if !fdbasedLinksEqual(args.FDBasedLinks, nil) {
		t.Errorf("FDBasedLinks mismatch:\ngot  %+v\nwant nil", args.FDBasedLinks)
	}

	if !loopbackLinksEqual(args.LoopbackLinks, nil) {
		t.Errorf("LoopbackLinks mismatch:\ngot  %+v\nwant nil", args.LoopbackLinks)
	}
}

func TestCollectLinksAndRoutes_LoopbackExtraRoutes(t *testing.T) {
	requireRoot(t)
	setupTestNamespace(t)
	setupLoopback(t)

	vethLink := createVethPair(t, "testveth0")
	if err := netlink.LinkSetUp(vethLink); err != nil {
		t.Fatalf("Failed to bring up interface: %v", err)
	}
	if err := netlink.AddrAdd(vethLink, &netlink.Addr{
		IPNet: &net.IPNet{IP: net.ParseIP("10.0.0.1"), Mask: net.CIDRMask(24, 32)},
	}); err != nil {
		t.Fatalf("Failed to add address: %v", err)
	}

	// Add a custom route pointing to the loopback interface.
	// This simulates routes added by e.g. podman-network-create --route.
	loLink, err := netlink.LinkByName("lo")
	if err != nil {
		t.Fatalf("Failed to get lo link: %v", err)
	}
	if err := netlink.RouteAdd(&netlink.Route{
		Dst:       parseCIDR(t, "10.88.0.0/16"),
		LinkIndex: loLink.Attrs().Index,
	}); err != nil {
		t.Fatalf("Failed to add route to lo: %v", err)
	}

	conf := &config.Config{
		XDP: config.XDP{Mode: config.XDPModeOff},
	}

	args, err := collectLinksAndRoutes(conf, false)
	if err != nil {
		t.Fatalf("collectLinksAndRoutes failed: %v", err)
	}

	wantLoopbackLinks := []boot.LoopbackLink{
		{
			Name: "lo",
			Addresses: []netip.Prefix{
				netip.MustParsePrefix("127.0.0.1/8"),
				netip.MustParsePrefix("::1/128"),
			},
			Routes: []boot.Route{
				{
					Destination: netip.MustParsePrefix("127.0.0.0/8"),
				},
				{
					Destination: netip.MustParsePrefix("::1/128"),
				},
				{
					Destination: netip.MustParsePrefix("10.88.0.0/16"),
				},
			},
		},
	}

	if !loopbackLinksEqual(args.LoopbackLinks, wantLoopbackLinks) {
		t.Errorf("LoopbackLinks mismatch:\ngot  %+v\nwant %+v", args.LoopbackLinks, wantLoopbackLinks)
	}

	wantFDLinks := []boot.FDBasedLink{
		{
			Name:        "testveth0",
			MTU:         1500,
			LinkAddress: vethLink.Attrs().HardwareAddr,
			QDisc:       config.QDiscNone,
			Addresses: []netip.Prefix{
				netip.MustParsePrefix("10.0.0.1/24"),
			},
			Routes: []boot.Route{
				{
					Destination: netip.MustParsePrefix("10.0.0.0/24"),
				},
			},
		},
	}
	if !fdbasedLinksEqual(args.FDBasedLinks, wantFDLinks) {
		t.Errorf("FDBasedLinks mismatch:\ngot  %+v\nwant %+v", args.FDBasedLinks, wantFDLinks)
	}
}

func TestDialExternalUDS(t *testing.T) {
	// The peer listens on SOCK_SEQPACKET, as an external network proxy must.
	dir := t.TempDir()
	path := filepath.Join(dir, "netproxy.sock")
	ln, err := net.Listen("unixpacket", path)
	if err != nil {
		t.Fatalf(`net.Listen("unixpacket", %q) failed: %v`, path, err)
	}
	defer ln.Close()

	accepted := make(chan net.Conn, 1)
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			accepted <- nil
			return
		}
		accepted <- conn
	}()

	f, err := dialExternalUDS(path)
	if err != nil {
		t.Fatalf("dialExternalUDS(%q) failed: %v", path, err)
	}
	defer f.Close()

	for _, opt := range []int{unix.SO_RCVBUF, unix.SO_SNDBUF} {
		sz, err := unix.GetsockoptInt(int(f.Fd()), unix.SOL_SOCKET, opt)
		if err != nil {
			t.Fatalf("GetsockoptInt(%d) failed: %v", opt, err)
		}
		if sz <= 0 {
			t.Errorf("GetsockoptInt(%d) = %d, want > 0", opt, sz)
		}
	}

	var peer net.Conn
	select {
	case peer = <-accepted:
		if peer == nil {
			t.Fatal("ln.Accept() failed")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for the peer to accept the connection")
	}
	defer peer.Close()

	// Message boundaries must be preserved in both directions: the sentry and
	// the external proxy exchange one IP packet per datagram.
	wantOutbound := []byte("outbound-packet")
	if _, err := f.Write(wantOutbound); err != nil {
		t.Fatalf("f.Write(%q) failed: %v", wantOutbound, err)
	}
	got := make([]byte, 64)
	if err := peer.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
		t.Fatalf("peer.SetReadDeadline() failed: %v", err)
	}
	n, err := peer.Read(got)
	if err != nil {
		t.Fatalf("peer.Read() failed: %v", err)
	}
	if !bytes.Equal(got[:n], wantOutbound) {
		t.Errorf("peer.Read() = %q, want %q", got[:n], wantOutbound)
	}

	wantInbound := []byte("inbound-packet")
	if _, err := peer.Write(wantInbound); err != nil {
		t.Fatalf("peer.Write(%q) failed: %v", wantInbound, err)
	}
	n, err = f.Read(got)
	if err != nil {
		t.Fatalf("f.Read() failed: %v", err)
	}
	if !bytes.Equal(got[:n], wantInbound) {
		t.Errorf("f.Read() = %q, want %q", got[:n], wantInbound)
	}
}

func TestConfigureProxyLink(t *testing.T) {
	link := boot.FDBasedLink{
		Name:              "eth0",
		NumChannels:       4,
		TXChecksumOffload: true,
		RXChecksumOffload: false,
	}
	configureProxyLink(&link)
	if !link.IsProxy {
		t.Errorf("configureProxyLink() IsProxy = false, want true")
	}
	if link.NumChannels != 1 {
		t.Errorf("configureProxyLink() NumChannels = %d, want 1", link.NumChannels)
	}
	if !link.RXChecksumOffload {
		t.Errorf("configureProxyLink() RXChecksumOffload = false, want true")
	}
}

func TestDialExternalUDSRejectsStreamPeer(t *testing.T) {
	// A SOCK_STREAM peer provides no message boundaries and must be rejected by
	// the kernel with EPROTOTYPE.
	dir := t.TempDir()
	path := filepath.Join(dir, "stream.sock")
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Fatalf(`net.Listen("unix", %q) failed: %v`, path, err)
	}
	defer ln.Close()

	f, err := dialExternalUDS(path)
	if err == nil {
		_ = f.Close()
		t.Fatalf("dialExternalUDS(%q) succeeded against a SOCK_STREAM listener, want error", path)
	}
	if !errors.Is(err, unix.EPROTOTYPE) {
		t.Errorf("dialExternalUDS(%q) = %v, want EPROTOTYPE", path, err)
	}
}

func TestDialExternalUDSMissingPath(t *testing.T) {
	path := filepath.Join(t.TempDir(), "does-not-exist.sock")
	f, err := dialExternalUDS(path)
	if err == nil {
		_ = f.Close()
		t.Fatalf("dialExternalUDS(%q) succeeded for a nonexistent socket, want error", path)
	}
	if !errors.Is(err, unix.ENOENT) {
		t.Errorf("dialExternalUDS(%q) = %v, want ENOENT", path, err)
	}
}

func TestPrimaryInterface(t *testing.T) {
	testCases := []struct {
		name    string
		args    *boot.CreateLinksAndRoutesArgs
		want    string
		wantErr bool
	}{
		{
			name:    "nil args",
			args:    nil,
			wantErr: true,
		},
		{
			name: "v4 gateway in fdbased",
			args: &boot.CreateLinksAndRoutesArgs{
				Defaultv4Gateway: boot.DefaultRoute{Name: "eth1"},
				FDBasedLinks:     []boot.FDBasedLink{{Name: "eth0"}, {Name: "eth1"}},
			},
			want: "eth1",
		},
		{
			name: "v4 gateway not in fdbased rejects eth0 fallback",
			args: &boot.CreateLinksAndRoutesArgs{
				Defaultv4Gateway: boot.DefaultRoute{Name: "other0"},
				FDBasedLinks:     []boot.FDBasedLink{{Name: "eth0"}, {Name: "eth1"}},
			},
			wantErr: true,
		},
		{
			name: "v6 gateway in fdbased",
			args: &boot.CreateLinksAndRoutesArgs{
				Defaultv6Gateway: boot.DefaultRoute{Name: "eth2"},
				FDBasedLinks:     []boot.FDBasedLink{{Name: "eth1"}, {Name: "eth2"}},
			},
			want: "eth2",
		},
		{
			name: "v6 gateway not in fdbased rejects single link fallback",
			args: &boot.CreateLinksAndRoutesArgs{
				Defaultv6Gateway: boot.DefaultRoute{Name: "other0"},
				FDBasedLinks:     []boot.FDBasedLink{{Name: "myif0"}},
			},
			wantErr: true,
		},
		{
			name: "v4 and v6 gateways match same fdbased link",
			args: &boot.CreateLinksAndRoutesArgs{
				Defaultv4Gateway: boot.DefaultRoute{Name: "eth1"},
				Defaultv6Gateway: boot.DefaultRoute{Name: "eth1"},
				FDBasedLinks:     []boot.FDBasedLink{{Name: "eth0"}, {Name: "eth1"}},
			},
			want: "eth1",
		},
		{
			name: "v4 and v6 gateways differ",
			args: &boot.CreateLinksAndRoutesArgs{
				Defaultv4Gateway: boot.DefaultRoute{Name: "eth0"},
				Defaultv6Gateway: boot.DefaultRoute{Name: "eth1"},
				FDBasedLinks:     []boot.FDBasedLink{{Name: "eth0"}, {Name: "eth1"}},
			},
			wantErr: true,
		},
		{
			name: "no gateway with eth0 fallback",
			args: &boot.CreateLinksAndRoutesArgs{
				FDBasedLinks: []boot.FDBasedLink{{Name: "eth0"}, {Name: "eth1"}},
			},
			want: "eth0",
		},
		{
			name: "single link in fdbased",
			args: &boot.CreateLinksAndRoutesArgs{
				FDBasedLinks: []boot.FDBasedLink{{Name: "tap0"}},
			},
			want: "tap0",
		},
		{
			name: "multiple links with no gateway and no eth0",
			args: &boot.CreateLinksAndRoutesArgs{
				FDBasedLinks: []boot.FDBasedLink{{Name: "tap0"}, {Name: "tap1"}},
			},
			wantErr: true,
		},
		{
			// The gateway resolves to an XDP link, which cannot be proxied over
			// UDS. primaryInterface() must reject it rather than falling back or
			// returning a name that has no corresponding FDBasedLink.
			name: "v4 gateway matches an XDP link only",
			args: &boot.CreateLinksAndRoutesArgs{
				Defaultv4Gateway: boot.DefaultRoute{Name: "xdp0"},
				XDPLinks:         []boot.XDPLink{{Name: "xdp0"}},
				FDBasedLinks:     []boot.FDBasedLink{{Name: "tap0"}, {Name: "tap1"}},
			},
			wantErr: true,
		},
		{
			// Even when eth0 is present in FDBasedLinks, a default gateway on an
			// XDP link must fail rather than silently routing default traffic
			// around the UDS proxy.
			name: "v4 gateway matches an XDP link rejects eth0 fallback",
			args: &boot.CreateLinksAndRoutesArgs{
				Defaultv4Gateway: boot.DefaultRoute{Name: "xdp0"},
				XDPLinks:         []boot.XDPLink{{Name: "xdp0"}},
				FDBasedLinks:     []boot.FDBasedLink{{Name: "eth0"}, {Name: "eth1"}},
			},
			wantErr: true,
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := primaryInterface(tc.args)
			if (err != nil) != tc.wantErr {
				t.Fatalf("primaryInterface(%+v) error = %v, wantErr %v", tc.args, err, tc.wantErr)
			}
			if got != tc.want {
				t.Errorf("primaryInterface(%+v) = %q, want %q", tc.args, got, tc.want)
			}
		})
	}
}
