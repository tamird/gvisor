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

package virtual_tcp_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"runtime"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/atomicbitops"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/waiter"
)

// completedClock exposes only the completion of the existing clock's callbacks.
// It lets this fixture drain immediate jobs created by a TCP processor without
// mistaking an empty link queue for completion of protocol processing.
type completedClock struct {
	*faketime.ManualClock
	callbacks atomicbitops.Uint64
}

func (c *completedClock) AfterFunc(d time.Duration, f func()) tcpip.Timer {
	return c.ManualClock.AfterFunc(d, func() {
		f()
		c.callbacks.Add(1)
	})
}

// zeroReader supplies reproducible ISN secrets and RNG seeds only for this
// isolated test. It has no mutable state and is safe for concurrent reads.
type zeroReader struct{}

func (zeroReader) Read(b []byte) (int, error) {
	clear(b)
	return len(b), nil
}

type packetObservation struct {
	AtNS           int64
	From           int
	Sequence       uint32
	Acknowledgment uint32
	Flags          uint8
	PayloadBytes   int
	PacketSHA256   string
	Dropped        bool
}

type delivery struct {
	at     time.Duration
	from   int
	packet *stack.PacketBuffer
	row    packetObservation
}

// TestVirtualTCPTransfer is a bounded feasibility test, not a congestion model:
// both TCP endpoints, ACKs, retransmissions and received bytes are real netstack
// operations. Only the link's delivery schedule and one loss are test inputs.
func TestVirtualTCPTransfer(t *testing.T) {
	if got, want := runtime.GOMAXPROCS(0), 1; got != want {
		t.Fatalf("GOMAXPROCS = %d, want %d for one TCP processor per stack", got, want)
	}
	for _, drop := range []bool{false, true} {
		name := "no_loss"
		if drop {
			name = "one_loss"
		}
		t.Run(name, func(t *testing.T) {
			runVirtualTCP(t, drop)
		})
	}
}

func runVirtualTCP(t *testing.T, drop bool) {
	t.Helper()
	const (
		quantum = time.Millisecond
		delay   = 10 * time.Millisecond
		rate    = int64(2500000) // 20 Mbit/s, independently in each direction.
		maxTime = 10 * time.Second
	)
	clock := &completedClock{ManualClock: faketime.NewManualClock()}
	var stacks [2]*stack.Stack
	var links [2]*channel.Endpoint
	var pending []delivery
	var nextTX [2]time.Duration
	var trace []packetObservation
	addresses := [2]tcpip.Address{tcpip.AddrFrom4([4]byte{10, 0, 0, 1}), tcpip.AddrFrom4([4]byte{10, 0, 0, 2})}
	t.Cleanup(func() {
		for _, s := range stacks {
			if s != nil {
				s.Destroy()
			}
		}
		for _, link := range links {
			if link != nil {
				link.Close()
			}
		}
		for _, packet := range pending {
			packet.packet.DecRef()
		}
	})
	for side := range stacks {
		s := stack.New(stack.Options{
			NetworkProtocols:   []stack.NetworkProtocolFactory{ipv4.NewProtocol},
			TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocolCUBIC},
			Clock:              clock,
			ClockResolution:    quantum,
			SecureRNG:          zeroReader{},
		})
		stacks[side] = s
		links[side] = channel.New(1024, 1500, "")
		if err := s.CreateNIC(1, links[side]); err != nil {
			t.Fatalf("CreateNIC(%d): %s", side, err)
		}
		addr := tcpip.ProtocolAddress{Protocol: ipv4.ProtocolNumber, AddressWithPrefix: addresses[side].WithPrefix()}
		if err := s.AddProtocolAddress(1, addr, stack.AddressProperties{}); err != nil {
			t.Fatalf("AddProtocolAddress(%d): %s", side, err)
		}
		s.SetRouteTable([]tcpip.Route{{Destination: header.IPv4EmptySubnet, NIC: 1}})
	}
	now := func() time.Duration { return clock.NowMonotonic().Sub(tcpip.MonotonicTime{}) }
	var dropped bool
	var lostSequence uint32
	var lostPayload string
	var recovered bool

	// Endpoint calls have returned before this fence; no application or link
	// goroutine can enqueue more work. With one processor per stack, Pause drains
	// its endpoint queue. A timer callback may enqueue another endpoint, so drain
	// immediate clock jobs at the same instant and repeat after resuming if any
	// callback ran. Time never advances while the processors are paused.
	fence := func() {
		t.Helper()
		for pass := 0; pass < 32; pass++ {
			generation := clock.callbacks.Load()
			clock.RunImmediatelyScheduledJobs()
			for _, s := range stacks {
				s.Pause()
			}
			stable := func() bool {
				defer func() {
					for _, s := range stacks {
						s.Resume()
					}
				}()
				clock.RunImmediatelyScheduledJobs()
				if clock.callbacks.Load() != generation {
					return false
				}
				for side, link := range links {
					for packet := link.Read(); packet != nil; packet = link.Read() {
						view := packet.ToView()
						ip := header.IPv4(view.AsSlice())
						if got, want := ip.TransportProtocol(), tcp.ProtocolNumber; got != want {
							view.Release()
							packet.DecRef()
							t.Fatalf("transport protocol = %d, want %d", got, want)
						}
						segment := header.TCP(ip.Payload())
						payload := segment.Payload()
						payloadHash := fmt.Sprintf("%x", sha256.Sum256(payload))
						isLoss := drop && !dropped && side == 0 && len(payload) != 0
						if isLoss {
							dropped, lostSequence, lostPayload = true, segment.SequenceNumber(), payloadHash
						} else if dropped && side == 0 && len(payload) != 0 && segment.SequenceNumber() == lostSequence && payloadHash == lostPayload {
							recovered = true
						}
						// Dropped packets still consume their serialization time.
						nextTX[side] = max(nextTX[side], now()) + time.Duration((int64(packet.Size())*int64(time.Second)+rate-1)/rate)
						at := nextTX[side] + delay
						row := packetObservation{AtNS: int64(at), From: side, Sequence: segment.SequenceNumber(), Acknowledgment: segment.AckNumber(), Flags: uint8(segment.Flags()), PayloadBytes: len(payload), PacketSHA256: fmt.Sprintf("%x", sha256.Sum256(view.AsSlice())), Dropped: isLoss}
						pending = append(pending, delivery{at: at, from: side, packet: stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: packet.ToBuffer()}), row: row})
						view.Release()
						packet.DecRef()
					}
				}
				return true
			}()
			if stable {
				return
			}
		}
		t.Fatal("TCP callbacks did not reach the bounded same-instant fence")
	}
	step := func() {
		t.Helper()
		fence()
		if now() >= maxTime {
			t.Fatalf("transfer exceeded %v virtual time; queued=%d", maxTime, len(pending))
		}
		next := now() + quantum
		index := -1
		for i, packet := range pending {
			if packet.at < next || (packet.at == next && index == -1) {
				next, index = packet.at, i
			}
		}
		clock.Advance(next - now())
		if index != -1 {
			packet := pending[index]
			pending = append(pending[:index], pending[index+1:]...)
			trace = append(trace, packet.row)
			if !packet.row.Dropped {
				links[1-packet.from].InjectInbound(ipv4.ProtocolNumber, packet.packet)
			}
			packet.packet.DecRef()
		}
		fence()
	}
	newEndpoint := func(side int) tcpip.Endpoint {
		t.Helper()
		var queue waiter.Queue
		ep, err := stacks[side].NewEndpoint(tcp.ProtocolNumber, ipv4.ProtocolNumber, &queue)
		if err != nil {
			t.Fatalf("NewEndpoint(%d): %s", side, err)
		}
		t.Cleanup(ep.Close)
		return ep
	}
	listener, client := newEndpoint(1), newEndpoint(0)
	serverAddr := tcpip.FullAddress{Addr: addresses[1], Port: 8080}
	if err := listener.Bind(serverAddr); err != nil {
		t.Fatalf("Bind(server): %s", err)
	}
	if err := listener.Listen(1); err != nil {
		t.Fatalf("Listen: %s", err)
	}
	if err := client.Bind(tcpip.FullAddress{Addr: addresses[0], Port: 40000}); err != nil {
		t.Fatalf("Bind(client): %s", err)
	}
	connectErr := client.Connect(serverAddr)
	if _, ok := connectErr.(*tcpip.ErrConnectStarted); !ok {
		t.Fatalf("Connect = %v, want ErrConnectStarted", connectErr)
	}
	var server tcpip.Endpoint
	for server == nil {
		step()
		ep, _, err := listener.Accept(nil)
		if _, ok := err.(*tcpip.ErrWouldBlock); ok {
			continue
		}
		if err != nil {
			t.Fatalf("Accept: %s", err)
		}
		server = ep
	}
	t.Cleanup(server.Close)
	if got, want := tcp.EndpointState(client.State()), tcp.StateEstablished; got != want {
		t.Fatalf("client state = %s, want %s", got, want)
	}
	data := make([]byte, 128<<10)
	for i := range data {
		data[i] = byte(i*17 + i/251)
	}
	if n, err := client.Write(bytes.NewReader(data), tcpip.WriteOptions{}); err != nil || n != int64(len(data)) {
		t.Fatalf("Write = (%d, %v), want (%d, nil)", n, err, len(data))
	}
	if err := client.Shutdown(tcpip.ShutdownWrite); err != nil {
		t.Fatalf("Shutdown(write): %s", err)
	}
	var received bytes.Buffer
	var eof bool
	for {
		step()
		_, err := server.Read(&received, tcpip.ReadOptions{})
		switch err.(type) {
		case nil, *tcpip.ErrWouldBlock:
		case *tcpip.ErrClosedForReceive:
			eof = true
		default:
			t.Fatalf("Read: %s", err)
		}
		fence()
		// FIN-WAIT-2 requires the real peer's cumulative ACK of our data and
		// FIN to have been processed, not merely emitted onto the fake link.
		if eof && tcp.EndpointState(client.State()) == tcp.StateFinWait2 && len(pending) == 0 {
			break
		}
	}
	if !bytes.Equal(received.Bytes(), data) {
		t.Fatalf("received %d bytes with SHA256 %x, want %d bytes with SHA256 %x", received.Len(), sha256.Sum256(received.Bytes()), len(data), sha256.Sum256(data))
	}
	retransmits := stacks[0].Stats().TCP.Retransmits.Value()
	if drop {
		if !dropped || !recovered || retransmits == 0 {
			t.Fatalf("loss recovery: dropped=%t, retransmitted exact payload=%t, retransmits=%d", dropped, recovered, retransmits)
		}
	} else if retransmits != 0 {
		t.Fatalf("no-loss retransmits = %d, want 0", retransmits)
	}
	for side, s := range stacks {
		if got, want := s.Stats().TCP.SegmentSendErrors.Value(), uint64(0); got != want {
			t.Fatalf("side %d segment send errors = %d, want %d", side, got, want)
		}
		if got, want := s.Stats().TCP.ChecksumErrors.Value(), uint64(0); got != want {
			t.Fatalf("side %d checksum errors = %d, want %d", side, got, want)
		}
	}
	encodedTrace, err := json.Marshal(trace)
	if err != nil {
		t.Fatal(err)
	}
	result := struct {
		Case           string
		VirtualNS      int64
		Bytes          int
		PayloadSHA256  string
		Retransmits    uint64
		RTOs           uint64
		SACKRecoveries uint64
		TraceSHA256    string
		Trace          []packetObservation
	}{
		Case:           t.Name(),
		VirtualNS:      int64(now()),
		Bytes:          received.Len(),
		PayloadSHA256:  fmt.Sprintf("%x", sha256.Sum256(received.Bytes())),
		Retransmits:    retransmits,
		RTOs:           stacks[0].Stats().TCP.Timeouts.Value(),
		SACKRecoveries: stacks[0].Stats().TCP.SACKRecovery.Value(),
		TraceSHA256:    fmt.Sprintf("%x", sha256.Sum256(encodedTrace)),
		Trace:          trace,
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("VIRTUAL_TCP %s", encoded)
}
