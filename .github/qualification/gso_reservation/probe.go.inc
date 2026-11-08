package tcp

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"io"
	"os"
	"strings"
	"testing"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/checksum"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv6"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/waiter"
)

type gsoWireLink struct {
	*channel.Endpoint
	observe func(*stack.PacketBuffer)
}

func (l *gsoWireLink) WritePackets(pkts stack.PacketBufferList) (int, tcpip.Error) {
	for _, p := range pkts.AsSlice() {
		if p.TransportProtocolNumber == ProtocolNumber {
			l.observe(p)
		}
	}
	return pkts.Len(), nil
}

// Use the production sender initialization and a peer MSS above the path MTU.
func gsoSender(tb testing.TB, option uint16) (*Endpoint, *gsoWireLink) {
	tb.Helper()
	exe, err := os.Open("/proc/self/exe")
	if err != nil { tb.Fatal(err) }
	digest := sha256.New()
	if _, err := io.Copy(digest, exe); err != nil { tb.Fatal(err) }
	if err := exe.Close(); err != nil { tb.Fatal(err) }
	status, err := os.ReadFile("/proc/self/status")
	if err != nil { tb.Fatal(err) }
	for _, line := range strings.Split(string(status), "\n") {
		if cpu, ok := strings.CutPrefix(line, "Cpus_allowed_list:"); ok {
			tb.Logf("GSO_OWNER sha=%x cpu=%s", digest.Sum(nil), strings.TrimSpace(cpu))
		}
	}
	link := &gsoWireLink{Endpoint: channel.New(1, 1500, ""), observe: func(*stack.PacketBuffer) {}}
	link.SupportedGSOKind = stack.GVisorGSOSupported
	s := stack.New(stack.Options{
		NetworkProtocols: []stack.NetworkProtocolFactory{ipv6.NewProtocol},
		TransportProtocols: []stack.TransportProtocolFactory{NewProtocol},
	})
	tb.Cleanup(func() { s.Destroy(); link.Close() })
	const nic = 1
	if err := s.CreateNICWithOptions(nic, link, stack.NICOptions{EnableExperimentIPOption: true}); err != nil {
		tb.Fatal(err)
	}
	local := tcpip.AddrFrom16([16]byte{0: 0x20, 1: 1, 15: 1})
	remote := tcpip.AddrFrom16([16]byte{0: 0x20, 1: 1, 15: 2})
	if err := s.AddProtocolAddress(nic, tcpip.ProtocolAddress{
		Protocol: ipv6.ProtocolNumber, AddressWithPrefix: tcpip.AddressWithPrefix{Address: local, PrefixLen: 64},
	}, stack.AddressProperties{}); err != nil {
		tb.Fatal(err)
	}
	s.SetRouteTable([]tcpip.Route{{Destination: header.IPv6EmptySubnet, NIC: nic}})
	route, routeErr := s.FindRoute(nic, local, remote, ipv6.ProtocolNumber, false)
	if routeErr != nil {
		tb.Fatal(routeErr)
	}
	tb.Cleanup(route.Release)
	var wq waiter.Queue
	socket, socketErr := s.NewEndpoint(ProtocolNumber, ipv6.ProtocolNumber, &wq)
	if socketErr != nil {
		tb.Fatal(socketErr)
	}
	e := socket.(*Endpoint)
	tb.Cleanup(e.Close)
	e.mu.Lock()
	e.route = route
	tb.Cleanup(func() { e.mu.Lock(); e.route = nil; e.mu.Unlock() })
	e.TransportEndpointInfo.ID = stack.TransportEndpointID{LocalAddress: local, RemoteAddress: remote, LocalPort: 1111, RemotePort: 2222}
	e.SocketOptions().SetExperimentOptionValue(option)
	e.initGSO()
	initSender(e, 1000, 2000, 65535, 65535, 0)
	e.mu.Unlock()
	if e.gso.Type != stack.GSOGvisor || e.gso.NeedsCsum || e.gso.MSS != 1440 {
		tb.Fatalf("unexpected software GSO configuration: %+v", e.gso)
	}
	return e, link
}

func sendGSOPayload(e *Endpoint, payload []byte, input **stack.PacketBuffer) tcpip.Error {
	p := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: buffer.MakeWithData(payload)})
	defer p.DecRef()
	*input = p
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.sendRaw(p, header.TCPFlagAck|header.TCPFlagPsh, 1001, 2001, 65535, nil)
}

func TestGSOExperimentReservation(t *testing.T) {
	for _, opt := range []uint16{0, 99} {
		t.Run(fmt.Sprintf("option_%d", opt), func(t *testing.T) {
			e, link := gsoSender(t, opt)
			mss := int(e.gso.MSS)
			payload := make([]byte, 7*mss+73)
			for i := range payload {
				payload[i] = byte(i*29 + i/251)
			}
			var input *stack.PacketBuffer
			var seen, offset int
			link.observe = func(p *stack.PacketBuffer) {
				v := p.ToView()
				defer v.Release()
				ip := header.IPv6(v.AsSlice())
				if !ip.IsValid(v.Size()) || ip.PayloadLength() != uint16(v.Size()-header.IPv6MinimumSize) || ip.SourceAddress() != e.route.LocalAddress() || ip.DestinationAddress() != e.route.RemoteAddress() {
					t.Fatalf("invalid IPv6 packet %d", seen)
				}
				wireTCP := ip.Payload()
				if opt != 0 {
					if ip.NextHeader() != uint8(header.IPv6ExperimentExtHdrIdentifier) || len(wireTCP) < 8 || wireTCP[0] != uint8(ProtocolNumber) || wireTCP[1] != 0 || binary.BigEndian.Uint16(wireTCP[2:4]) != opt {
						t.Fatalf("invalid IPv6 experiment header at %d", seen)
					}
					wireTCP = wireTCP[header.IPv6ExperimentHdrLength:]
				} else if ip.NextHeader() != uint8(ProtocolNumber) {
					t.Fatalf("unexpected extension at %d", seen)
				}
				tcp := header.TCP(wireTCP)
				size := min(mss, len(payload)-offset)
				if size <= 0 || len(wireTCP) != header.TCPMinimumSize+size || tcp.DataOffset() != header.TCPMinimumSize ||
					tcp.SequenceNumber() != 1001+uint32(offset) || tcp.AckNumber() != 2001 || tcp.SourcePort() != 1111 || tcp.DestinationPort() != 2222 ||
					tcp.Flags() != header.TCPFlagAck|header.TCPFlagPsh || !bytes.Equal(tcp.Payload(), payload[offset:offset+size]) ||
					!tcp.IsChecksumValid(ip.SourceAddress(), ip.DestinationAddress(), checksum.Checksum(tcp.Payload(), 0), uint16(size)) {
					t.Fatalf("invalid wire TCP sequence, flags, length, checksum, or data at %d", seen)
				}
				last := offset+size == len(payload)
				if (p == input) != last { t.Fatalf("packet %d: input reused=%t, final=%t", seen, p == input, last) }
				t.Logf("GSO_PACKET opt=%d index=%d offset=%d data=%d reserve=%d available=%d wire=%d checksum=%04x reused=%t", opt, seen, offset, size, p.ReservedHeaderBytes(), p.AvailableHeaderBytes(), v.Size(), tcp.Checksum(), p == input)
				offset += size
				seen++
			}
			if err := sendGSOPayload(e, payload, &input); err != nil { t.Fatal(err) }
			if seen != 8 || offset != len(payload) { t.Fatalf("got %d packets, %d bytes", seen, offset) }
		})
	}
}

func BenchmarkIPv6SoftwareGSO(b *testing.B) {
	for _, opt := range []uint16{0, 99} {
		b.Run(fmt.Sprintf("option_%d", opt), func(b *testing.B) {
			e, link := gsoSender(b, opt)
			payload := make([]byte, 32000)
			var input *stack.PacketBuffer
			var reserved, packets int64
			link.observe = func(p *stack.PacketBuffer) {
				reserved += int64(p.ReservedHeaderBytes())
				packets++
			}
			b.SetBytes(int64(len(payload)))
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				if err := sendGSOPayload(e, payload, &input); err != nil { b.Fatal(err) }
			}
			b.StopTimer()
			if want := int64(b.N)*int64((len(payload)+int(e.gso.MSS)-1)/int(e.gso.MSS)); packets != want {
				b.Fatalf("saw %d emitted packets, want %d", packets, want)
			}
			b.ReportMetric(float64(reserved)/float64(b.N), "reserved-B/op")
		})
	}
}
