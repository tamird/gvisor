// Fork diagnostic for a routed connection from an inner network namespace.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"strconv"
	"syscall"
	"time"

	"github.com/vishvananda/netlink"
)

const mtu = 1466

func must(op string, err error) {
	if err != nil {
		panic(fmt.Sprintf("%s: %v", op, err))
	}
}
func dev(name string) netlink.Link {
	l, err := netlink.LinkByName(name)
	must("lookup "+name, err)
	return l
}

// IP MTUs in this experiment must match. Sentry's bridge SetMTU stores
// the link value while MTU subtracts the Ethernet header; Linux reports
// exactly what was set. Read back the effective value on each interface.
func setIPMTU(name string) {
	must("set MTU "+name, netlink.LinkSetMTU(dev(name), mtu))
	if got := dev(name).Attrs().MTU; got == mtu-14 {
		must("set link MTU "+name, netlink.LinkSetMTU(dev(name), mtu+14))
	}
	if got := dev(name).Attrs().MTU; got != mtu {
		panic(fmt.Sprintf("effective MTU %s = %d, want %d", name, got, mtu))
	}
}

func up(name string) {
	must("up "+name, netlink.LinkSetUp(dev(name)))
}
func address(name, cidr string) {
	a, err := netlink.ParseAddr(cidr)
	must("parse "+cidr, err)
	must("address "+name, netlink.AddrAdd(dev(name), a))
}
func inner() {
	var signal [1]byte
	_, err := io.ReadFull(os.Stdin, signal[:])
	must("wait for inner link", err)
	up("lo")
	setIPMTU("cin")
	up("cin")
	address("cin", "198.51.100.2/30")
	must("child default route", netlink.RouteAdd(&netlink.Route{LinkIndex: dev("cin").Attrs().Index, Gw: net.ParseIP("198.51.100.1").To4()}))
	start := time.Now()
	r := map[string]any{"inner": dev("cin").Attrs().Name, "mtu": dev("cin").Attrs().MTU}
	c, err := net.DialTimeout("tcp4", "192.0.2.1:14011", 4*time.Second)
	if err != nil {
		r["error"] = err.Error()
	} else {
		must("deadline", c.SetReadDeadline(time.Now().Add(9*time.Second)))
		h := sha256.New()
		n, err := io.Copy(h, c)
		c.Close()
		r["size"] = n
		r["sha256"] = hex.EncodeToString(h.Sum(nil))
		if err != nil {
			r["error"] = err.Error()
		}
	}
	r["seconds"] = time.Since(start).Seconds()
	must("result", json.NewEncoder(os.Stdout).Encode(r))
}
func main() {
	if len(os.Args) > 1 && os.Args[1] == "inner" {
		inner()
		return
	}
	must("enable IPv4 forwarding", os.WriteFile("/proc/sys/net/ipv4/ip_forward", []byte("1\n"), 0))
	self, err := os.Executable()
	must("executable", err)
	child := exec.Command(self, "inner")
	child.SysProcAttr = &syscall.SysProcAttr{Cloneflags: syscall.CLONE_NEWNET}
	child.Stdout = os.Stdout
	child.Stderr = os.Stderr
	w, err := child.StdinPipe()
	must("child gate", err)
	must("start nested network namespace", child.Start())
	defer func() { child.Process.Kill(); child.Wait() }()
	ns, err := os.Open("/proc/" + strconv.Itoa(child.Process.Pid) + "/ns/net")
	must("open inner network namespace", err)
	defer ns.Close()
	a := netlink.NewLinkAttrs()
	a.Name = "br0"
	a.MTU = mtu
	must("create bridge", netlink.LinkAdd(&netlink.Bridge{LinkAttrs: a}))
	a = netlink.NewLinkAttrs()
	a.Name = "bin"
	a.MTU = mtu
	must("create veth pair", netlink.LinkAdd(&netlink.Veth{LinkAttrs: a, PeerName: "cin", PeerNamespace: netlink.NsFd(ns.Fd())}))
	must("enslave veth", netlink.LinkSetMaster(dev("bin"), dev("br0")))
	setIPMTU("br0")
	setIPMTU("bin")
	up("br0")
	up("bin")
	address("br0", "198.51.100.1/30")
	// Read back the forwarding relationship before traffic is allowed.
	outer := dev("bin").Attrs()
	br := dev("br0").Attrs()
	if outer.MasterIndex != br.Index || br.MTU != mtu {
		panic(fmt.Sprintf("wrong bridge: bin master %d, bridge %d, mtu %d", outer.MasterIndex, br.Index, br.MTU))
	}
	fmt.Printf("bridge-ready master=%d mtu=%d\n", outer.MasterIndex, br.MTU)
	_, err = w.Write([]byte{1})
	must("release child", err)
	w.Close()
	must("wait child", child.Wait())
}
