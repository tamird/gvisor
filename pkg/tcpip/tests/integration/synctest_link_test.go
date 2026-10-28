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
	if mode := os.Getenv("TCP_LINK_CHILD"); mode != "" {
		n, err := strconv.Atoi(os.Getenv("GOMAXPROCS"))
		if err != nil || n != runtime.GOMAXPROCS(0) {
			t.Fatalf("GOMAXPROCS=%d, want %d", runtime.GOMAXPROCS(0), n)
		}
		synctest.Test(t, func(t *testing.T) { linkTrace(t, mode == "drop") })
		return
	}
	bin, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, mode := range []string{"deliver", "drop"} {
		t.Run(mode, func(t *testing.T) {
			var want string
			for _, n := range []int{1, 2} {
				for trial := range 2 {
					ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
					cmd := exec.CommandContext(ctx, bin, "-test.run=^TestTCPVirtualLink$", "-test.v", "-test.timeout=7s")
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
						t.Fatalf("different TCP schedules:\n%s\n%s", want, trace)
					}
					want = trace
				}
			}
			t.Logf("MATCHED_SCHEDULE mode=%s traces=4", mode)
		})
	}
}

type hop struct {
	AtNS     int64
	Side     int
	Event    string
	Seq, Ack uint32
	Flags    header.TCPFlags
	Data     string
	TCPHash  string
}

// Both dispatchers are parked before the clock advances. Each delivered packet
// resumes only its receiver. Pause drains that receiver's enqueued endpoint;
// Wait settles immediate Go work with the dispatcher parked. Each wave below
// then checks the expected protocol observation before allowing time to move.
func linkTrace(t *testing.T, drop bool) {
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
	client := newEP(0)
	if err := client.Bind(tcpip.FullAddress{Addr: addr[0], Port: 40000}); err != nil {
		t.Fatal(err)
	}
	if _, ok := client.Connect(remote).(*tcpip.ErrConnectStarted); !ok {
		t.Fatal("expected pending connection")
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
		ports := [2]uint16{40000, 8080}
		if ip.SourceAddress() != addr[i] || ip.DestinationAddress() != addr[1-i] || ip.TransportProtocol() != tcp.ProtocolNumber || sg.SourcePort() != ports[i] || sg.DestinationPort() != ports[1-i] {
			t.Fatalf("unexpected link tuple on %d", i)
		}
		h := hop{time.Since(start).Nanoseconds(), i, event, sg.SequenceNumber(), sg.AckNumber(), sg.Flags(), string(sg.Payload()), fmt.Sprintf("%x", sha256.Sum256(ip.Payload()))}
		trace = append(trace, h)
		fmt.Printf("HOP mode_drop=%t %+v\n", drop, h)
		return p, h
	}
	forward := func(p *stack.PacketBuffer, i int) {
		t.Helper()
		defer p.DecRef()
		advance(delay)
		// The receiving stack must parse the bytes as a new wire packet; the
		// transmitter has already marked its network and transport headers.
		in := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: p.ToBuffer()})
		defer in.DecRef()
		valid := sides[1-i].Stats().TCP.ValidSegmentsReceived
		before := valid.Value()
		links[1-i].InjectInbound(ipv4.ProtocolNumber, in)
		freeze(1 - i)
		if n := valid.Value() - before; n != 1 {
			t.Fatalf("receiver %d accepted %d TCP segments for one wire delivery", 1-i, n)
		}
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
	p, h := pull(0, "syn")
	if h.Flags != header.TCPFlagSyn {
		t.Fatal(h)
	}
	forward(p, 0)
	p, h = pull(1, "synack")
	if h.Flags != header.TCPFlagSyn|header.TCPFlagAck || h.Ack != trace[0].Seq+1 {
		t.Fatal(h)
	}
	forward(p, 1)
	p, h = pull(0, "ack")
	if h.Flags != header.TCPFlagAck || h.Ack != trace[1].Seq+1 {
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
	var info tcpip.TCPInfoOption
	if err := client.GetSockOpt(&info); err != nil || info.RTO <= delay {
		t.Fatalf("TCP RTO %s: %v", info.RTO, err)
	}
	if n, err := client.Write(strings.NewReader(payload), tcpip.WriteOptions{}); err != nil || n != int64(len(payload)) {
		t.Fatalf("write: %d %v", n, err)
	}
	freeze(0)
	p, original := pull(0, "data")
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
	b, errJSON := json.Marshal(trace)
	if errJSON != nil {
		t.Fatal(errJSON)
	}
	fmt.Printf("WIRE_TRACE %s\n", b)
}
