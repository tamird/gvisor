package wire_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"sync"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/fdbased"
	"gvisor.dev/gvisor/pkg/tcpip/network/arp"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv6"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/waiter"
)

const etherAll = 0x0300 // htons(ETH_P_ALL) on the Linux AMD64 runner.

type txPacket struct {
	IPBytes, NetHeader, TCPHeader, Payload int
	Seq                                    uint32
	SynMSS                                 uint16
	Flags                                  header.TCPFlags
	GSO                                    stack.GSO
}
type txCall struct {
	Packets        []txPacket
	Sent           int
	Error          string
	BeginUS, EndUS int64
}
type writer struct {
	stack.LinkEndpoint
	mu    sync.Mutex
	calls []txCall
}

func (w *writer) GSOMaxSize() uint32 { return w.LinkEndpoint.(stack.GSOEndpoint).GSOMaxSize() }
func (w *writer) SupportedGSO() stack.SupportedGSO {
	return w.LinkEndpoint.(stack.GSOEndpoint).SupportedGSO()
}
func (w *writer) WritePackets(list stack.PacketBufferList) (int, tcpip.Error) {
	c := txCall{}
	for _, p := range list.AsSlice() {
		if p.TransportProtocolNumber != tcp.ProtocolNumber {
			continue
		}
		h := header.TCP(p.TransportHeader().Slice())
		if len(h) < header.TCPMinimumSize {
			continue
		} // IP fragments may lack TCP headers.
		row := txPacket{
			IPBytes: p.Size() - len(p.LinkHeader().Slice()), NetHeader: len(p.NetworkHeader().Slice()), TCPHeader: len(h),
			Payload: p.Data().Size(), Seq: h.SequenceNumber(), Flags: h.Flags(), GSO: p.GSOOptions,
		}
		if h.Flags().Contains(header.TCPFlagSyn) {
			row.SynMSS = header.ParseSynOptions(h.Options(), h.Flags().Contains(header.TCPFlagAck)).MSS
		}
		c.Packets = append(c.Packets, row)
	}
	c.BeginUS = time.Now().UnixMicro()
	n, err := w.LinkEndpoint.WritePackets(list)
	c.EndUS = time.Now().UnixMicro()
	c.Sent = n
	if err != nil {
		c.Error = err.String()
	}
	if len(c.Packets) > 0 || err != nil {
		w.mu.Lock()
		if len(w.calls) < 128 {
			w.calls = append(w.calls, c)
		}
		w.mu.Unlock()
	}
	return n, err
}
func (w *writer) take() []txCall {
	w.mu.Lock()
	defer w.mu.Unlock()
	cs := w.calls
	w.calls = nil
	return cs
}

func packetFD(iface *net.Interface, vnet bool) (int, error) {
	fd, err := unix.Socket(unix.AF_PACKET, unix.SOCK_RAW|unix.SOCK_CLOEXEC, etherAll)
	if err != nil {
		return -1, err
	}
	if err = unix.Bind(fd, &unix.SockaddrLinklayer{Protocol: etherAll, Ifindex: iface.Index}); err == nil && vnet {
		err = unix.SetsockoptInt(fd, unix.SOL_PACKET, unix.PACKET_VNET_HDR, 1)
	}
	if err != nil {
		unix.Close(fd)
		return -1, err
	}
	return fd, nil
}

type node struct {
	s        *stack.Stack
	link     *writer
	ip       tcpip.Address
	vnet, fd int
}

func openNode(t *testing.T, iface, other *net.Interface, addr, peer tcpip.Address, proto tcpip.NetworkProtocolNumber, mode string) (node, error) {
	fd, err := packetFD(iface, mode == "host")
	if err != nil {
		return node{}, err
	}
	t.Cleanup(func() { unix.Close(fd) })
	vnet, err := unix.GetsockoptInt(fd, unix.SOL_PACKET, unix.PACKET_VNET_HDR)
	if err != nil {
		return node{}, err
	}
	var size uint32
	if mode != "none" {
		size = 32768
	}
	ep, err := fdbased.New(&fdbased.Options{FDs: []int{fd}, MTU: uint32(iface.MTU), EthernetHeader: true,
		Address: tcpip.LinkAddress(iface.HardwareAddr), PacketDispatchMode: fdbased.RecvMMsg,
		GSOMaxSize: size, GVisorGSOEnabled: mode == "software", ProcessorsPerChannel: 1})
	if err != nil {
		return node{}, err
	}
	w := &writer{LinkEndpoint: ep}
	s := stack.New(stack.Options{NetworkProtocols: []stack.NetworkProtocolFactory{arp.NewProtocol, ipv4.NewProtocol, ipv6.NewProtocol}, TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol}})
	t.Cleanup(s.Destroy)
	const nic = 1
	if nerr := s.CreateNICWithOptions(nic, w, stack.NICOptions{EnableExperimentIPOption: true}); nerr != nil {
		return node{}, fmt.Errorf("CreateNIC: %s", nerr)
	}
	mssOpt := tcpip.TCPSACKEnabled(false)
	if nerr := s.SetTransportProtocolOption(tcp.ProtocolNumber, &mssOpt); nerr != nil {
		return node{}, fmt.Errorf("SACK: %s", nerr)
	}
	prefix, subnet := 24, header.IPv4EmptySubnet
	if proto == ipv6.ProtocolNumber {
		prefix, subnet = 64, header.IPv6EmptySubnet
	}
	if nerr := s.AddProtocolAddress(nic, tcpip.ProtocolAddress{Protocol: proto, AddressWithPrefix: tcpip.AddressWithPrefix{Address: addr, PrefixLen: prefix}}, stack.AddressProperties{}); nerr != nil {
		return node{}, fmt.Errorf("address: %s", nerr)
	}
	s.SetRouteTable([]tcpip.Route{{Destination: subnet, NIC: nic}})
	if nerr := s.AddStaticNeighbor(nic, proto, peer, tcpip.LinkAddress(other.HardwareAddr)); nerr != nil {
		return node{}, fmt.Errorf("neighbor: %s", nerr)
	}
	return node{s, w, addr, vnet, fd}, nil
}

type frame struct {
	Bytes  []byte
	Status uint32
	Kind   uint8
}

func capture(t *testing.T, fd int, mac net.HardwareAddr) []frame {
	t.Helper()
	var out []frame
	b := make([]byte, 65536)
	oob := make([]byte, 256)
	for reads := 0; reads < 2048; reads++ {
		n, o, flags, sa, err := unix.Recvmsg(fd, b, oob, unix.MSG_DONTWAIT)
		if err == unix.EAGAIN || err == unix.EWOULDBLOCK {
			return out
		}
		if err != nil {
			t.Fatal(err)
		}
		addr, ok := sa.(*unix.SockaddrLinklayer)
		if !ok || addr.Pkttype == unix.PACKET_OUTGOING || n < 14 || !bytes.Equal(b[6:12], mac) {
			continue
		}
		if flags&(unix.MSG_TRUNC|unix.MSG_CTRUNC) != 0 {
			t.Fatal("truncated physical capture")
		}
		ether := binary.BigEndian.Uint16(b[12:14])
		if ether != uint16(ipv4.ProtocolNumber) && ether != uint16(ipv6.ProtocolNumber) {
			continue
		}
		msgs, err := unix.ParseSocketControlMessage(oob[:o])
		if err != nil {
			t.Fatal(err)
		}
		var status uint32
		var found bool
		for _, msg := range msgs {
			if msg.Header.Level == unix.SOL_PACKET && msg.Header.Type == unix.PACKET_AUXDATA && len(msg.Data) >= 4 {
				status = binary.LittleEndian.Uint32(msg.Data)
				found = true
			}
		}
		if !found {
			t.Fatal("missing peer PACKET_AUXDATA")
		}
		if len(out) >= 120 {
			t.Fatal("too many peer frames for one sample")
		}
		out = append(out, frame{append([]byte(nil), b[:n]...), status, addr.Pkttype})
	}
	t.Fatal("peer frame drain exceeded bound")
	return nil
}

type result struct {
	Family, Mode, Phase                   string
	PeerMSS, SelectedGSO, Vnet, SourceFD  int
	SrcPort, DstPort                      uint16
	Connected, Accepted                   bool
	Wrote, Read                           int
	Digest, Expected, Outcome, StageError string
	Calls                                 []txCall
	HandshakeCalls                        []txCall
	PeerHandshakeCalls                    []txCall
	Frames, Handshake                     []frame
	PrimeFrames                           []frame
	PrimeCalls                            []txCall
	PrimeWrote, PrimeRead                 int
}

func trial(t *testing.T, family, mode, phase string, mss, index int) {
	t.Helper()
	r := result{Family: family, Mode: mode, Phase: phase, PeerMSS: mss, SrcPort: uint16(31000 + index), DstPort: uint16(32000 + index), Outcome: "setup_failure"}
	defer func() {
		b, err := json.Marshal(r)
		if err != nil {
			t.Fatal(err)
		}
		t.Logf("MTU_RESULT %s", b)
	}()
	a, err := net.InterfaceByName("mtua")
	if err != nil {
		t.Fatal(err)
	}
	b, err := net.InterfaceByName("mtub")
	if err != nil {
		t.Fatal(err)
	}
	if a.MTU != 1500 || b.MTU != 1500 {
		t.Fatalf("MTUs %d %d", a.MTU, b.MTU)
	}
	sip := tcpip.AddrFrom4([4]byte{192, 0, 2, 1})
	dip := tcpip.AddrFrom4([4]byte{192, 0, 2, 2})
	proto := ipv4.ProtocolNumber
	if family == "v6" {
		proto = ipv6.ProtocolNumber
		sip = tcpip.AddrFrom16([16]byte{0: 0x20, 1: 1, 2: 0x0d, 3: 0xb8, 15: 1})
		dip = tcpip.AddrFrom16([16]byte{0: 0x20, 1: 1, 2: 0x0d, 3: 0xb8, 15: 2})
	}
	capfd, err := packetFD(b, false)
	if err != nil {
		r.StageError = err.Error()
		return
	}
	t.Cleanup(func() { unix.Close(capfd) })
	if err := unix.SetsockoptInt(capfd, unix.SOL_PACKET, unix.PACKET_AUXDATA, 1); err != nil {
		r.StageError = err.Error()
		return
	}
	src, err := openNode(t, a, b, sip, dip, proto, mode)
	if err != nil {
		r.StageError = "source: " + err.Error()
		return
	}
	dst, err := openNode(t, b, a, dip, sip, proto, "none")
	if err != nil {
		r.StageError = "peer: " + err.Error()
		return
	}
	r.SelectedGSO = int(src.link.SupportedGSO())
	r.Vnet = src.vnet
	r.SourceFD = src.fd
	var lwq, cwq waiter.Queue
	listen, ne := dst.s.NewEndpoint(tcp.ProtocolNumber, proto, &lwq)
	if ne != nil {
		t.Fatal(ne)
	}
	t.Cleanup(listen.Close)
	if mss > 0 {
		if ne := listen.SetSockOptInt(tcpip.MaxSegOption, mss); ne != nil {
			t.Fatal(ne)
		}
	}
	if ne := listen.Bind(tcpip.FullAddress{NIC: 1, Addr: dip, Port: r.DstPort}); ne != nil {
		t.Fatal(ne)
	}
	if ne := listen.Listen(8); ne != nil {
		t.Fatal(ne)
	}
	client, ne := src.s.NewEndpoint(tcp.ProtocolNumber, proto, &cwq)
	if ne != nil {
		t.Fatal(ne)
	}
	t.Cleanup(client.Close)
	ready, ch := waiter.NewChannelEntry(waiter.WritableEvents)
	cwq.EventRegister(&ready)
	defer cwq.EventUnregister(&ready)
	if ne := client.Bind(tcpip.FullAddress{NIC: 1, Addr: sip, Port: r.SrcPort}); ne != nil {
		t.Fatal(ne)
	}
	ne = client.Connect(tcpip.FullAddress{NIC: 1, Addr: dip, Port: r.DstPort})
	if _, ok := ne.(*tcpip.ErrConnectStarted); ok {
		select {
		case <-ch:
			ne = client.LastError()
		case <-time.After(2 * time.Second):
			r.StageError = "connect deadline"
			r.Handshake = capture(t, capfd, a.HardwareAddr)
			r.HandshakeCalls = src.link.take()
			return
		}
	}
	if ne != nil {
		r.StageError = ne.String()
		return
	}
	r.Connected = true
	readyA, accepted := waiter.NewChannelEntry(waiter.ReadableEvents)
	lwq.EventRegister(&readyA)
	defer lwq.EventUnregister(&readyA)
	peer, pwq, ne := listen.Accept(nil)
	if _, ok := ne.(*tcpip.ErrWouldBlock); ok {
		select {
		case <-accepted:
			peer, pwq, ne = listen.Accept(nil)
		case <-time.After(2 * time.Second):
			r.StageError = "accept deadline"
			return
		}
	}
	if ne != nil {
		r.StageError = ne.String()
		return
	}
	socket := gonet.NewTCPConn(pwq, peer)
	t.Cleanup(func() { socket.Close() })
	r.Accepted = true
	r.Handshake = capture(t, capfd, a.HardwareAddr)
	r.HandshakeCalls = src.link.take()
	r.PeerHandshakeCalls = dst.link.take()
	client.SocketOptions().SetDelayOption(false)
	if phase != "off" {
		client.SocketOptions().SetExperimentOptionValue(99)
	}
	var missingPrefix []byte
	if phase == "off_again" {
		prime := []byte("gvisor-toggle-witness")
		w, writeErr := client.Write(bytes.NewReader(prime), tcpip.WriteOptions{})
		if writeErr != nil || w != int64(len(prime)) {
			t.Fatalf("marker write = %d, %v", w, writeErr)
		}
		buf := make([]byte, len(prime))
		socket.SetReadDeadline(time.Now().Add(450 * time.Millisecond))
		n, _ := io.ReadFull(socket, buf)
		if !bytes.Equal(buf[:n], prime[:n]) {
			t.Fatal("incorrect toggle marker")
		}
		r.PrimeWrote = int(w)
		r.PrimeRead = n
		r.PrimeCalls = src.link.take()
		r.PrimeFrames = capture(t, capfd, a.HardwareAddr)
		missingPrefix = prime[n:]
		client.SocketOptions().SetExperimentOptionValue(0)
	}
	payload := make([]byte, 7*1440+73)
	for i := range payload {
		payload[i] = byte(i*29 + i/251 + index)
	}
	expected := append(append([]byte{}, missingPrefix...), payload...)
	r.Expected = fmt.Sprintf("%x", sha256.Sum256(expected))
	count, ne := client.Write(bytes.NewReader(payload), tcpip.WriteOptions{})
	r.Wrote = int(count)
	if ne != nil {
		r.StageError = "write: " + ne.String()
	}
	got := make([]byte, len(expected))
	socket.SetReadDeadline(time.Now().Add(1650 * time.Millisecond))
	n, readErr := io.ReadFull(socket, got)
	r.Read = n
	r.Digest = fmt.Sprintf("%x", sha256.Sum256(got[:n]))
	if readErr == nil && r.Wrote == len(payload) && bytes.Equal(got, expected) {
		r.Outcome = "delivered"
	} else {
		r.Outcome = "undelivered"
		if readErr != nil {
			r.StageError = readErr.Error()
		}
	}
	r.Calls = src.link.take()
	r.Frames = capture(t, capfd, a.HardwareAddr)
}

func TestExperimentMTUWire(t *testing.T) {
	if os.Getenv("GSO_PRIVATE_WIRE") != "1" {
		t.Fatal("requires bounded private veth wrapper")
	}
	index := 0
	for _, mode := range []string{"none", "software", "host"} {
		for _, family := range []string{"v4", "v6"} {
			for _, mss := range []int{0, 1200} {
				for _, phase := range []string{"off", "on", "off_again"} {
					name := fmt.Sprintf("%s/%s/mss_%d/%s", mode, family, mss, phase)
					t.Run(name, func(t *testing.T) { trial(t, family, mode, phase, mss, index) })
					index++
				}
			}
		}
	}
}
