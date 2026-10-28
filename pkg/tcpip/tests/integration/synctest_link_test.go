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

package synctest_tcp_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/waiter"
)

// Run independent processes so each stack chooses its processors at startup.
// Compare the entire observed schedule, including TCP headers and payload.
func TestTCPVirtualLink(t *testing.T) {
	runLinkModes(t, []string{"deliver", "drop"})
}

func TestTCPVirtualMiddleSACK(t *testing.T) {
	runLinkModes(t, []string{"reno", "cubic"})
}

func TestTCPVirtualTwoFlows(t *testing.T) {
	runLinkModes(t, []string{"two-forward", "two-reverse"})
}

func runLinkModes(t *testing.T, modes []string) {
	name := t.Name()
	if mode := os.Getenv("TCP_LINK_CHILD"); mode != "" {
		n, err := strconv.Atoi(os.Getenv("GOMAXPROCS"))
		if err != nil || n != runtime.GOMAXPROCS(0) {
			t.Fatalf("GOMAXPROCS=%d, want %d", runtime.GOMAXPROCS(0), n)
		}
		synctest.Test(t, func(t *testing.T) { linkTrace(t, mode) })
		return
	}
	bin, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, mode := range modes {
		t.Run(mode, func(t *testing.T) {
			var want string
			for _, n := range []int{1, 2} {
				for trial := range 2 {
					ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
					cmd := exec.CommandContext(ctx, bin, "-test.run=^"+name+"$", "-test.v", "-test.timeout=7s")
					for _, pair := range os.Environ() {
						key, _, _ := strings.Cut(pair, "=")
						switch key {
						case "GO_TEST_WRAP", "GO_TEST_WRAP_TESTV", "XML_OUTPUT_FILE", "TESTBRIDGE_TEST_ONLY", "GOMAXPROCS", "GOTRACEBACK":
						default:
							cmd.Env = append(cmd.Env, pair)
						}
					}
					cmd.Env = append(cmd.Env, "TCP_LINK_CHILD="+mode, fmt.Sprintf("GOMAXPROCS=%d", n), "GOTRACEBACK=all")
					out, err := cmd.CombinedOutput()
					cancel()
					t.Logf("LINK_RUN mode=%s processors=%d trial=%d exit=%v\n%s", mode, n, trial, err, out)
					if err != nil {
						t.Fatalf("child failed: %v", err)
					}
					var trace string
					for _, line := range strings.Split(string(out), "\n") {
						if value, ok := strings.CutPrefix(line, "WIRE_TRACE "); ok {
							if trace != "" {
								t.Fatal("duplicate wire trace")
							}
							trace = value
						}
					}
					if trace == "" {
						t.Fatal("no complete trace")
					}
					if want != "" && want != trace {
						t.Errorf("different TCP schedules:\n%s\n%s", want, trace)
					}
					want = trace
				}
			}
			if !t.Failed() {
				t.Logf("MATCHED_SCHEDULE mode=%s traces=4", mode)
			}
		})
	}
}

type hop struct {
	AtNS          int64
	Side          int
	ClientPort    uint16
	Event         string
	Seq, Ack      uint32
	Flags         header.TCPFlags
	Data          string
	TCPHash       string
	SACKPermitted bool               `json:",omitempty"`
	SACK          []header.SACKBlock `json:",omitempty"`
}

// Both dispatchers are parked before the clock advances. Each delivered packet
// resumes only its receiver. Pause drains that receiver's enqueued endpoint;
// Wait settles immediate Go work with the dispatcher parked. Each wave below
// then checks the expected protocol observation before allowing time to move.
func linkTrace(t *testing.T, mode string) {
	drop := mode == "drop"
	middle := mode == "reno" || mode == "cubic"
	two := mode == "two-forward" || mode == "two-reverse"
	const delay = 10 * time.Millisecond
	const payload = "one deliberately isolated segment"
	start := time.Now()
	addr := [2]tcpip.Address{tcpip.AddrFrom4([4]byte{10, 0, 0, 1}), tcpip.AddrFrom4([4]byte{10, 0, 0, 2})}
	var sides [2]*stack.Stack
	var links [2]*channel.Endpoint
	var eps []tcpip.Endpoint
	defer func() {
		for _, s := range sides {
			if s != nil {
				s.Resume()
			}
		}
		for i := len(eps) - 1; i >= 0; i-- {
			eps[i].Close()
		}
		for _, s := range sides {
			if s != nil {
				s.Destroy()
			}
		}
		for _, l := range links {
			if l != nil {
				l.Close()
			}
		}
	}()
	for i := range sides {
		s := stack.New(stack.Options{NetworkProtocols: []stack.NetworkProtocolFactory{ipv4.NewProtocol}, TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol}, Clock: tcpip.NewStdClock(), SecureRNG: zeroReader{}})
		sides[i] = s
		l := channel.New(32, 1500, "")
		links[i] = l
		if err := s.CreateNIC(1, l); err != nil {
			t.Fatal(err)
		}
		if err := s.AddProtocolAddress(1, tcpip.ProtocolAddress{Protocol: ipv4.ProtocolNumber, AddressWithPrefix: addr[i].WithPrefix()}, stack.AddressProperties{}); err != nil {
			t.Fatal(err)
		}
		s.SetRouteTable([]tcpip.Route{{Destination: header.IPv4EmptySubnet, NIC: 1}})
		// Use ordinary timeout recovery so the dropped tail cannot be repaired
		// by a tail-loss probe before the endpoint's reported RTO.
		var recovery tcpip.TCPRecovery
		if err := s.SetTransportProtocolOption(tcp.ProtocolNumber, &recovery); err != nil {
			t.Fatal(err)
		}
		if middle {
			enabled := tcpip.TCPSACKEnabled(true)
			if err := s.SetTransportProtocolOption(tcp.ProtocolNumber, &enabled); err != nil {
				t.Fatal(err)
			}
		}
		s.Pause()
	}
	freeze := func(i int) {
		now := time.Now()
		sides[i].Resume()
		sides[i].Pause()
		synctest.Wait()
		if !time.Now().Equal(now) {
			t.Fatal("time advanced while draining receiver")
		}
	}
	advance := func(d time.Duration) {
		now := time.Now()
		synctest.Wait()
		time.Sleep(d)
		synctest.Wait()
		if time.Since(now) != d {
			t.Fatalf("advanced %s, want %s", time.Since(now), d)
		}
	}
	newEP := func(i int) tcpip.Endpoint {
		ep, err := sides[i].NewEndpoint(tcp.ProtocolNumber, ipv4.ProtocolNumber, new(waiter.Queue))
		if err != nil {
			t.Fatal(err)
		}
		eps = append(eps, ep)
		return ep
	}
	listener := newEP(1)
	remote := tcpip.FullAddress{Addr: addr[1], Port: 8080}
	if err := listener.Bind(remote); err != nil {
		t.Fatal(err)
	}
	if err := listener.Listen(2); err != nil {
		t.Fatal(err)
	}
	var trace []hop
	pull := func(i int, event string) (*stack.PacketBuffer, hop) {
		t.Helper()
		p := links[i].Read()
		if p == nil {
			t.Fatalf("missing %s packet on %d at %s; trace=%+v", event, i, time.Since(start), trace)
		}
		v := p.ToView()
		defer v.Release()
		ip := header.IPv4(v.AsSlice())
		sg := header.TCP(ip.Payload())
		clientPort, serverPort := sg.SourcePort(), sg.DestinationPort()
		if i == 1 {
			clientPort, serverPort = serverPort, clientPort
		}
		if ip.SourceAddress() != addr[i] || ip.DestinationAddress() != addr[1-i] || ip.TransportProtocol() != tcp.ProtocolNumber || serverPort != remote.Port || (clientPort != 40000 && (!two || clientPort != 40001)) {
			t.Fatalf("unexpected link tuple on %d", i)
		}
		h := hop{AtNS: time.Since(start).Nanoseconds(), Side: i, ClientPort: clientPort, Event: event, Seq: sg.SequenceNumber(), Ack: sg.AckNumber(), Flags: sg.Flags(), Data: string(sg.Payload()), TCPHash: fmt.Sprintf("%x", sha256.Sum256(ip.Payload()))}
		if middle {
			if h.Flags&header.TCPFlagSyn != 0 {
				h.SACKPermitted = header.ParseSynOptions(sg.Options(), h.Flags&header.TCPFlagAck != 0).SACKPermitted
			} else {
				h.SACK = sg.ParsedOptions().SACKBlocks
			}
		}
		trace = append(trace, h)
		fmt.Printf("HOP mode=%s %+v\n", mode, h)
		return p, h
	}
	enqueue := func(p *stack.PacketBuffer, i int) {
		t.Helper()
		defer p.DecRef()
		// The receiving stack must parse the bytes as a new wire packet; the
		// transmitter has already marked its network and transport headers.
		in := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: p.ToBuffer()})
		defer in.DecRef()
		valid := sides[1-i].Stats().TCP.ValidSegmentsReceived
		before := valid.Value()
		links[1-i].InjectInbound(ipv4.ProtocolNumber, in)
		if n := valid.Value() - before; n != 1 {
			t.Fatalf("receiver %d accepted %d TCP segments for one wire delivery", 1-i, n)
		}
	}
	inject := func(p *stack.PacketBuffer, i int) {
		enqueue(p, i)
		freeze(1 - i)
	}
	forward := func(p *stack.PacketBuffer, i int) {
		advance(delay)
		inject(p, i)
	}
	none := func() {
		t.Helper()
		for i, l := range links {
			if p := l.Read(); p != nil {
				p.DecRef()
				t.Fatalf("unexpected queued packet on %d, trace=%+v", i, trace)
			}
		}
	}
	connect := func(port uint16) (tcpip.Endpoint, tcpip.Endpoint) {
		client := newEP(0)
		if middle {
			cc := tcpip.CongestionControlOption(mode)
			if err := client.SetSockOpt(&cc); err != nil {
				t.Fatal(err)
			}
		}
		if err := client.Bind(tcpip.FullAddress{Addr: addr[0], Port: port}); err != nil {
			t.Fatal(err)
		}
		if _, ok := client.Connect(remote).(*tcpip.ErrConnectStarted); !ok {
			t.Fatal("expected pending connection")
		}
		p, h := pull(0, "syn")
		syn := h
		if h.Flags != header.TCPFlagSyn || h.ClientPort != port {
			t.Fatal(h)
		}
		forward(p, 0)
		p, h = pull(1, "synack")
		if h.Flags != header.TCPFlagSyn|header.TCPFlagAck || h.ClientPort != port || h.Ack != syn.Seq+1 {
			t.Fatal(h)
		}
		synAck := h
		forward(p, 1)
		p, h = pull(0, "ack")
		if h.Flags != header.TCPFlagAck || h.ClientPort != port || h.Ack != synAck.Seq+1 {
			t.Fatal(h)
		}
		forward(p, 0)
		server, _, err := listener.Accept(nil)
		if err != nil {
			t.Fatal(err)
		}
		eps = append(eps, server)
		for _, ep := range []tcpip.Endpoint{client, server} {
			if tcp.EndpointState(ep.State()) != tcp.StateEstablished {
				t.Fatal("handshake incomplete")
			}
		}
		none()
		return client, server
	}
	client, server := connect(40000)
	if two {
		second, peer := connect(40001)
		twoFlows(t, mode, [2]tcpip.Endpoint{client, second}, [2]tcpip.Endpoint{server, peer}, delay, pull, enqueue, freeze, advance, none)
		writeTrace(t, trace)
		return
	}
	var info tcpip.TCPInfoOption
	if err := client.GetSockOpt(&info); err != nil || info.RTO <= delay {
		t.Fatalf("TCP RTO %s: %v", info.RTO, err)
	}
	if middle {
		if !trace[0].SACKPermitted || !trace[1].SACKPermitted {
			t.Fatal("SACK was not negotiated")
		}
		middleSACK(t, mode, client, server, sides[0].Stats().TCP, info, delay,
			pull, inject, advance, func() { freeze(0) }, none)
		writeTrace(t, trace)
		return
	}
	if n, err := client.Write(strings.NewReader(payload), tcpip.WriteOptions{}); err != nil || n != int64(len(payload)) {
		t.Fatalf("write: %d %v", n, err)
	}
	freeze(0)
	p, original := pull(0, "data")
	var h hop
	if original.Data != payload || original.Seq != trace[0].Seq+1 {
		t.Fatal(original)
	}
	var got bytes.Buffer
	stats := sides[0].Stats().TCP
	if drop {
		p.DecRef() // The only intentionally lost packet.
		advance(info.RTO - time.Nanosecond)
		if _, err := server.Read(&got, tcpip.ReadOptions{}); err == nil {
			t.Fatal("server received dropped payload")
		} else if _, ok := err.(*tcpip.ErrWouldBlock); !ok {
			t.Fatal(err)
		}
		none()
		if stats.Timeouts.Value() != 0 || stats.Retransmits.Value() != 0 {
			t.Fatal("early loss recovery")
		}
		advance(time.Nanosecond)
		freeze(0)
		p, h = pull(0, "rto")
		if h.Seq != original.Seq || h.Data != original.Data || stats.Timeouts.Value() != 1 || stats.Retransmits.Value() != 1 {
			t.Fatalf("retransmission=%+v timeouts=%d retransmits=%d", h, stats.Timeouts.Value(), stats.Retransmits.Value())
		}
	}
	forward(p, 0)
	if n, err := server.Read(&got, tcpip.ReadOptions{}); err != nil || n.Count != len(payload) || got.String() != payload {
		t.Fatalf("read %v %v %q", n, err, got.String())
	}
	if _, err := server.Read(&got, tcpip.ReadOptions{}); err == nil {
		t.Fatal("duplicate bytes")
	} else if _, ok := err.(*tcpip.ErrWouldBlock); !ok {
		t.Fatal(err)
	}
	freeze(1)
	p, h = pull(1, "data-ack")
	if h.Data != "" || h.Flags != header.TCPFlagAck || h.Ack != original.Seq+uint32(len(payload)) {
		t.Fatal(h)
	}
	forward(p, 1)
	none()
	// A captured ACK must also reach the sender: cross even the backoff
	// deadline and ensure no further copy of the payload is transmitted.
	advance(2*info.RTO + delay)
	none()
	var wantTimeouts uint64
	if drop {
		wantTimeouts = 1
	}
	if stats.Timeouts.Value() != wantTimeouts || stats.Retransmits.Value() != wantTimeouts {
		t.Fatalf("after ACK: timeouts=%d retransmits=%d, want %d", stats.Timeouts.Value(), stats.Retransmits.Value(), wantTimeouts)
	}
	fmt.Printf("DELIVERY drop=%t bytes=%d ack=%d timeouts=%d retransmits=%d RTO=%s\n", drop, got.Len(), h.Ack, stats.Timeouts.Value(), stats.Retransmits.Value(), info.RTO)
	writeTrace(t, trace)
}

// Both clients share a NIC. Queue one segment from each flow on the receiving
// stack before resuming its TCP workers; capture replies in actual wire order.
func twoFlows(t *testing.T, mode string, clients, servers [2]tcpip.Endpoint, delay time.Duration,
	pull func(int, string) (*stack.PacketBuffer, hop), enqueue func(*stack.PacketBuffer, int),
	freeze func(int), advance func(time.Duration), none func()) {
	t.Helper()
	order := [2]int{0, 1}
	if mode == "two-reverse" {
		order = [2]int{1, 0}
	}
	payload := [2]string{"data from port 40000", "different data from port 40001"}
	for _, i := range order {
		if n, err := clients[i].Write(strings.NewReader(payload[i]), tcpip.WriteOptions{}); err != nil || n != int64(len(payload[i])) {
			t.Fatalf("flow %d write %d, %v", i, n, err)
		}
	}
	freeze(0)
	var packets [2]*stack.PacketBuffer
	var ends [2]uint32
	var sent [2]bool
	for i := range packets {
		p, h := pull(0, "shared-data")
		flow := int(h.ClientPort) - 40000
		if h.Data != payload[flow] || sent[flow] {
			t.Fatalf("wrong or duplicate outgoing data: %+v", h)
		}
		sent[flow] = true
		ends[flow] = h.Seq + uint32(len(h.Data))
		packets[i] = p
	}
	none()
	advance(delay)
	for _, p := range packets {
		enqueue(p, 0)
	}
	freeze(1)
	for i, server := range servers {
		var got bytes.Buffer
		if n, err := server.Read(&got, tcpip.ReadOptions{}); err != nil || got.String() != payload[i] || n.Count != len(payload[i]) {
			t.Fatalf("flow %d read %v %q, %v", i, n, got.String(), err)
		}
		_, err := server.Read(&got, tcpip.ReadOptions{})
		if _, blocked := err.(*tcpip.ErrWouldBlock); !blocked {
			t.Fatalf("flow %d subsequent read returned %v, want would block", i, err)
		}
	}
	freeze(1)
	var acked [2]bool
	for i := range packets {
		p, h := pull(1, "shared-ack")
		flow := int(h.ClientPort) - 40000
		if h.Flags != header.TCPFlagAck || h.Data != "" || acked[flow] || h.Ack != ends[flow] {
			t.Fatalf("wrong or duplicate ack: %+v", h)
		}
		acked[flow] = true
		packets[i] = p
	}
	none()
	advance(delay)
	for _, p := range packets {
		enqueue(p, 1)
	}
	freeze(0)
	var maxRTO time.Duration
	for _, ep := range clients {
		var info tcpip.TCPInfoOption
		if err := ep.GetSockOpt(&info); err != nil || info.RTO <= delay {
			t.Fatalf("RTO %s, %v", info.RTO, err)
		}
		maxRTO = max(maxRTO, info.RTO)
	}
	advance(2*maxRTO + delay)
	freeze(0)
	none()
}

func writeTrace(t *testing.T, trace []hop) {
	t.Helper()
	b, errJSON := json.Marshal(trace)
	if errJSON != nil {
		t.Fatal(errJSON)
	}
	fmt.Printf("WIRE_TRACE %s\n", b)
}

// All five segments leave on one flight. Deliver the prefix, drop segment 1,
// and let the peer report segments 2–4. Advance once per network hop, not
// once per packet: packets in the same flight have the same link delay.
func middleSACK(t *testing.T, mode string, client, server tcpip.Endpoint, stats tcpip.TCPStats, info tcpip.TCPInfoOption, delay time.Duration,
	pull func(int, string) (*stack.PacketBuffer, hop), inject func(*stack.PacketBuffer, int), advance func(time.Duration), drainSender func(), none func()) {
	t.Helper()
	var actualCC tcpip.CongestionControlOption
	if err := client.GetSockOpt(&actualCC); err != nil || string(actualCC) != mode {
		t.Fatalf("congestion control = %q, %v; want %s", actualCC, err, mode)
	}
	const flight = 5
	mss := int(info.SndMss)
	if mss < 1 || info.SndCwnd < flight {
		t.Fatalf("insufficient flight: MSS=%d cwnd=%d", mss, info.SndCwnd)
	}
	var data strings.Builder
	for i := range flight {
		data.WriteString(strings.Repeat(string(byte('a'+i)), mss))
	}
	payload := data.String()
	if n, err := client.Write(strings.NewReader(payload), tcpip.WriteOptions{}); err != nil || n != int64(len(payload)) {
		t.Fatalf("write %d, %v; want %d", n, err, len(payload))
	}
	drainSender()
	packets := make([]*stack.PacketBuffer, flight)
	var first, lost hop
	for i := range flight {
		p, h := pull(0, "flight")
		packets[i] = p
		if i == 0 {
			first = h
		}
		if i == 1 {
			lost = h
		}
		if h.Seq != first.Seq+uint32(i*mss) || h.Data != payload[i*mss:(i+1)*mss] || h.AtNS != first.AtNS {
			t.Fatalf("noncontiguous flight segment %d: %+v", i, h)
		}
	}
	none()
	packets[1].DecRef() // Drop only the second segment.
	advance(delay)
	var acks []*stack.PacketBuffer
	for _, i := range []int{0, 2, 3, 4} {
		inject(packets[i], 0)
		p, ack := pull(1, "sack-ack")
		if ack.Flags != header.TCPFlagAck || ack.Ack != lost.Seq || ack.Data != "" {
			t.Fatalf("cumulative ACK after segment %d: %+v", i, ack)
		}
		if i == 0 {
			if len(ack.SACK) != 0 {
				t.Fatalf("unexpected SACK on prefix: %v", ack.SACK)
			}
		} else if len(ack.SACK) != 1 || uint32(ack.SACK[0].Start) != lost.Seq+uint32(mss) || uint32(ack.SACK[0].End) != first.Seq+uint32((i+1)*mss) {
			t.Fatalf("wrong SACK range after segment %d: %v", i, ack.SACK)
		}
		acks = append(acks, p)
	}
	var got bytes.Buffer
	readAvailable := func() {
		t.Helper()
		for {
			n, err := server.Read(&got, tcpip.ReadOptions{})
			if _, ok := err.(*tcpip.ErrWouldBlock); ok {
				return
			}
			if err != nil || n.Count == 0 {
				t.Fatalf("read %v, %v", n, err)
			}
		}
	}
	readAvailable()
	if got.String() != payload[:mss] {
		t.Fatalf("read %d bytes before gap filled, want only first %d", got.Len(), mss)
	}
	advance(delay)
	for i, p := range acks {
		inject(p, 1)
		if i < len(acks)-1 {
			none()
		}
	}
	repair, h := pull(0, "fast-retransmit")
	if h.Seq != lost.Seq || h.Data != lost.Data || h.AtNS-lost.AtNS >= int64(info.RTO) {
		t.Fatalf("unexpected retransmission: seq=%d len=%d elapsed=%s RTO=%s", h.Seq, len(h.Data), time.Duration(h.AtNS-lost.AtNS), info.RTO)
	}
	var recovered tcpip.TCPInfoOption
	if err := client.GetSockOpt(&recovered); err != nil || recovered.CcState != tcpip.SACKRecovery {
		t.Fatalf("expected SACK recovery before repair delivery, got %+v: %v", recovered, err)
	}
	advance(delay)
	inject(repair, 0)
	res, combined := pull(1, "cumulative-ack")
	if want := first.Seq + uint32(len(payload)); combined.Ack != want || combined.Flags != header.TCPFlagAck || len(combined.SACK) != 0 {
		t.Fatalf("after repair got %+v, want cumulative ACK %d", combined, want)
	}
	readAvailable()
	if got.String() != payload {
		t.Fatalf("after repair got %d bytes (%x), want %d (%x)", got.Len(), sha256.Sum256(got.Bytes()), len(payload), sha256.Sum256([]byte(payload)))
	}
	advance(delay)
	inject(res, 1)
	none()
	advance(info.RTO + delay)
	drainSender()
	none()
	readAvailable()
	if got.Len() != len(payload) || stats.Timeouts.Value() != 0 || stats.Retransmits.Value() != 1 ||
		stats.SACKRecovery.Value() != 1 || stats.FastRetransmit.Value() != 1 {
		t.Fatalf("bytes=%d timeouts=%d retransmits=%d SACK recoveries=%d fast retransmits=%d",
			got.Len(), stats.Timeouts.Value(), stats.Retransmits.Value(), stats.SACKRecovery.Value(), stats.FastRetransmit.Value())
	}
	fmt.Printf("MIDDLE_SACK controller=%s mss=%d bytes=%d sha256=%x ack=%d timeout=%d rexmit=%d sack=%d fast=%d recovery_at_ns=%d rto_ns=%d cwnd=%d recovered_cwnd=%d\n",
		actualCC, mss, got.Len(), sha256.Sum256(got.Bytes()), combined.Ack, stats.Timeouts.Value(), stats.Retransmits.Value(), stats.SACKRecovery.Value(), stats.FastRetransmit.Value(), h.AtNS, int64(info.RTO), info.SndCwnd, recovered.SndCwnd)
}
