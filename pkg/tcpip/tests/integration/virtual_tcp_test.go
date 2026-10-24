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
	CapturedAtNS   int64
	From           int
	Sequence       uint32
	EndSequence    uint32
	Acknowledgment uint32
	Flags          uint8
	PayloadBytes   int
	PacketSHA256   string
	PayloadSHA256  string
	OptionsHex     string
	SACKBlocks     []header.SACKBlock
	SACKPermitted  bool
	Retransmitted  bool
	Dropped        bool
}

type delivery struct {
	at     time.Duration
	from   int
	packet *stack.PacketBuffer
	row    packetObservation
}

type recoveryObservation struct {
	AtNS           int64
	RTOs           uint64
	SACKRecoveries uint64
	TLPRecoveries  uint64
	TimerCallbacks uint64
	FastRetransmit uint64
	Retransmits    uint64
}

// TestVirtualTCPTransfer compares finite real Reno and CUBIC transfers. The
// clock and explicit link schedule are controlled inputs, not a Linux or
// long-lived congestion model.
func TestVirtualTCPTransfer(t *testing.T) {
	if got, want := runtime.GOMAXPROCS(0), 1; got != want {
		t.Fatalf("GOMAXPROCS = %d, want %d for one TCP processor per stack", got, want)
	}
	for _, algorithm := range []struct {
		name     string
		protocol stack.TransportProtocolFactory
	}{
		{"reno", tcp.NewProtocol},
		{"cubic", tcp.NewProtocolCUBIC},
	} {
		t.Run(algorithm.name, func(t *testing.T) {
			for _, loss := range []struct {
				name   string
				offset int
			}{
				{"no_loss", -1},
				{"first_data_loss", 0},
				{"middle_loss", 64 << 10},
			} {
				t.Run(loss.name, func(t *testing.T) {
					runVirtualTCP(t, algorithm.name, algorithm.protocol, loss.offset)
				})
			}
		})
	}
}

func runVirtualTCP(t *testing.T, algorithm string, protocol stack.TransportProtocolFactory, dropOffset int) {
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
	var progress []recoveryObservation
	var received bytes.Buffer
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
			TransportProtocols: []stack.TransportProtocolFactory{protocol},
			Clock:              clock,
			ClockResolution:    quantum,
			SecureRNG:          zeroReader{},
		})
		stacks[side] = s
		var sack tcpip.TCPSACKEnabled
		if err := s.TransportProtocolOption(tcp.ProtocolNumber, &sack); err != nil || !sack {
			t.Fatalf("side %d SACK = %t, error = %v", side, sack, err)
		}
		var recovery tcpip.TCPRecovery
		if err := s.TransportProtocolOption(tcp.ProtocolNumber, &recovery); err != nil || recovery != tcpip.TCPRACKLossDetection {
			t.Fatalf("side %d recovery = %d, error = %v", side, recovery, err)
		}
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
	var lostSequence, lostEnd, dataSequence uint32
	var dataStarted bool
	var highestSent, acknowledged uint32
	var lostPayload string
	var recovered bool
	classification := "unclassified"
	var senderAlgorithm tcpip.CongestionControlOption

	defer func() {
		encodedTrace, err := json.Marshal(trace)
		if err != nil {
			t.Fatal(err)
		}
		result := struct {
			Case           string
			Algorithm      string
			DropOffset     int
			LostSequence   uint32
			LostEnd        uint32
			RecoveryTrace  []recoveryObservation
			VirtualNS      int64
			Bytes          int
			PayloadSHA256  string
			Retransmits    uint64
			RTOs           uint64
			SACKRecoveries uint64
			TLPRecoveries  uint64
			RecoveryClass  string
			TraceSHA256    string
			Trace          []packetObservation
		}{
			Case:      t.Name(),
			Algorithm: string(senderAlgorithm), DropOffset: dropOffset, LostSequence: lostSequence, LostEnd: lostEnd, RecoveryTrace: progress,
			VirtualNS:      int64(now()),
			Bytes:          received.Len(),
			PayloadSHA256:  fmt.Sprintf("%x", sha256.Sum256(received.Bytes())),
			Retransmits:    stacks[0].Stats().TCP.Retransmits.Value(),
			RTOs:           stacks[0].Stats().TCP.Timeouts.Value(),
			SACKRecoveries: stacks[0].Stats().TCP.SACKRecovery.Value(),
			TLPRecoveries:  stacks[0].Stats().TCP.TLPRecovery.Value(), RecoveryClass: classification,
			TraceSHA256: fmt.Sprintf("%x", sha256.Sum256(encodedTrace)),
			Trace:       trace,
		}
		encoded, err := json.Marshal(result)
		if err != nil {
			t.Fatal(err)
		}
		t.Logf("VIRTUAL_TCP %s", encoded)
	}()

	// Endpoint calls have returned before this fence; no application or link
	// goroutine can enqueue more work. With one processor per stack, Pause drains
	// its endpoint queue. A timer callback may enqueue another endpoint, so drain
	// immediate clock jobs at the same instant and repeat after resuming if any
	// callback ran. The driver advances time only with both processors paused.
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
				stats := stacks[0].Stats().TCP
				observation := recoveryObservation{
					AtNS: int64(now()), RTOs: stats.Timeouts.Value(),
					SACKRecoveries: stats.SACKRecovery.Value(),
					TLPRecoveries:  stats.TLPRecovery.Value(), TimerCallbacks: clock.callbacks.Load(),
					FastRetransmit: stats.FastRetransmit.Value(),
					Retransmits:    stats.Retransmits.Value(),
				}
				changed := len(progress) == 0
				if !changed {
					previous := progress[len(progress)-1]
					previous.AtNS = observation.AtNS
					changed = previous != observation
				}
				if changed {
					progress = append(progress, observation)
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
						var retransmitted, isLoss bool
						if side == 0 && len(payload) != 0 {
							if !dataStarted {
								dataSequence, dataStarted = segment.SequenceNumber(), true
							}
							offset := segment.SequenceNumber() - dataSequence
							retransmitted = offset < highestSent
							highestSent = max(highestSent, offset+uint32(len(payload)))
							isLoss = dropOffset >= 0 && !dropped && !retransmitted && int(offset) <= dropOffset && dropOffset < int(offset)+len(payload)
							if isLoss {
								dropped, lostSequence, lostPayload = true, segment.SequenceNumber(), payloadHash
								lostEnd = lostSequence + uint32(len(payload))
							} else if dropped && segment.SequenceNumber() == lostSequence && segment.SequenceNumber()+uint32(len(payload)) == lostEnd && payloadHash == lostPayload {
								recovered = true
							}
						}
						var sackPermitted bool
						if segment.Flags().Contains(header.TCPFlagSyn) {
							sackPermitted = header.ParseSynOptions(segment.Options(), segment.Flags().Contains(header.TCPFlagAck)).SACKPermitted
						}

						// Dropped packets still consume their serialization time.
						nextTX[side] = max(nextTX[side], now()) + time.Duration((int64(packet.Size())*int64(time.Second)+rate-1)/rate)
						at := nextTX[side] + delay
						row := packetObservation{
							AtNS: int64(at), CapturedAtNS: int64(now()), From: side,
							Sequence: segment.SequenceNumber(), EndSequence: segment.SequenceNumber() + uint32(len(payload)),
							Acknowledgment: segment.AckNumber(), Flags: uint8(segment.Flags()), PayloadBytes: len(payload),
							PacketSHA256: fmt.Sprintf("%x", sha256.Sum256(view.AsSlice())), PayloadSHA256: payloadHash,
							OptionsHex: fmt.Sprintf("%x", segment.Options()), SACKBlocks: header.ParseTCPOptions(segment.Options()).SACKBlocks,
							SACKPermitted: sackPermitted, Retransmitted: retransmitted, Dropped: isLoss,
						}
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
		func() {
			for _, s := range stacks {
				s.Pause()
			}
			defer func() {
				for _, s := range stacks {
					s.Resume()
				}
			}()
			// Timer callbacks may enqueue a TCP processor. Keep it parked
			// until Advance reaches the end of this bounded tick, so it
			// cannot observe a scheduler-dependent intermediate clock value.
			clock.Advance(next - now())
		}()
		// Timer-generated output is observed at this tick's end. The link
		// schedule models that explicit 1 ms resolution, not exact emission.
		fence()
		if index != -1 {
			packet := pending[index]
			pending = append(pending[:index], pending[index+1:]...)
			trace = append(trace, packet.row)
			if !packet.row.Dropped {
				links[1-packet.from].InjectInbound(ipv4.ProtocolNumber, packet.packet)
				if packet.from == 1 && dataStarted && header.TCPFlags(packet.row.Flags).Contains(header.TCPFlagAck) {
					acknowledged = max(acknowledged, packet.row.Acknowledgment-dataSequence)
				}
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
	// Endpoint.Listen takes the queue capacity directly. Capacity two leaves
	// room for this SYN without selecting the option-limited SYN-cookie path.
	if err := listener.Listen(2); err != nil {
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
	checkAlgorithm := func() {
		t.Helper()
		if err := client.GetSockOpt(&senderAlgorithm); err != nil || string(senderAlgorithm) != algorithm {
			t.Fatalf("connected sender algorithm = %q, error = %v, want %q", senderAlgorithm, err, algorithm)
		}
	}
	checkAlgorithm()
	var syns [2]int
	for _, packet := range trace {
		if header.TCPFlags(packet.Flags).Contains(header.TCPFlagSyn) {
			if !packet.SACKPermitted {
				t.Fatalf("side %d SYN did not negotiate SACK: %+v", packet.From, packet)
			}
			syns[packet.From]++
		}
	}
	if syns != [2]int{1, 1} {
		t.Fatalf("SYN/SYN-ACK census = %v, want [1 1]", syns)
	}
	data := make([]byte, 128<<10)
	for i := range data {
		data[i] = byte(i*17 + i/251)
	}
	const prefix = 128
	if n, err := client.Write(bytes.NewReader(data[:prefix]), tcpip.WriteOptions{}); err != nil || n != prefix {
		t.Fatalf("prefix Write = (%d, %v), want (%d, nil)", n, err, prefix)
	}
	for received.Len() != prefix || acknowledged < prefix {
		step()
		if _, err := server.Read(&received, tcpip.ReadOptions{}); err != nil {
			if _, ok := err.(*tcpip.ErrWouldBlock); !ok {
				t.Fatalf("prefix Read: %s", err)
			}
		}
		fence()
		if received.Len() > prefix {
			t.Fatalf("prefix Read delivered %d bytes, want %d", received.Len(), prefix)
		}
	}
	if n, err := client.Write(bytes.NewReader(data[prefix:]), tcpip.WriteOptions{}); err != nil || n != int64(len(data)-prefix) {
		t.Fatalf("bulk Write = (%d, %v), want (%d, nil)", n, err, len(data)-prefix)
	}
	if err := client.Shutdown(tcpip.ShutdownWrite); err != nil {
		t.Fatalf("Shutdown(write): %s", err)
	}
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
	checkAlgorithm()
	retransmits := stacks[0].Stats().TCP.Retransmits.Value()
	if dropOffset >= 0 {
		if !dropped || !recovered || retransmits == 0 {
			t.Fatalf("loss recovery: dropped=%t, retransmitted exact payload=%t, retransmits=%d", dropped, recovered, retransmits)
		}
	} else if retransmits != 0 {
		t.Fatalf("no-loss retransmits = %d, want 0", retransmits)
	}
	stats := stacks[0].Stats().TCP
	if dropOffset < 0 {
		classification = "no_loss"
	} else {
		var retransmissionNS int64
		for _, packet := range trace {
			if packet.From == 0 && packet.Retransmitted && packet.Sequence == lostSequence && packet.EndSequence == lostEnd {
				retransmissionNS = packet.CapturedAtNS
				break
			}
		}
		var higherData, holeSACK bool
		for _, packet := range trace {
			if packet.AtNS >= retransmissionNS || packet.Dropped {
				continue
			}
			if packet.From == 0 && packet.PayloadBytes > 0 && packet.Sequence >= lostEnd {
				higherData = true
			}
			if packet.From == 1 && packet.Acknowledgment == lostSequence {
				for _, block := range packet.SACKBlocks {
					if uint32(block.Start) >= lostEnd && block.Start.LessThan(block.End) {
						holeSACK = true
					}
				}
			}
		}
		if dropOffset == 0 && higherData {
			t.Fatal("higher data was delivered before prefix recovery")
		}
		if dropOffset > 0 && (!higherData || !holeSACK) {
			t.Fatalf("middle hole [%d,%d) retransmitted at %d without prior delivered higher data/SACK: %t/%t", lostSequence, lostEnd, retransmissionNS, higherData, holeSACK)
		}
		var before recoveryObservation
		for _, observation := range progress {
			if observation.AtNS <= retransmissionNS {
				before = observation
			}
		}
		switch {
		case before.RTOs > 0:
			classification = "rto"
		case stats.TLPRecovery.Value() > 0 && stats.Timeouts.Value() == 0:
			classification = "tail_loss_probe_recovery"
		case holeSACK && before.SACKRecoveries > 0 && stats.Timeouts.Value() == 0:
			classification = "sack_recovery"
		}
	}

	for side, s := range stacks {
		if got, want := s.Stats().TCP.SegmentSendErrors.Value(), uint64(0); got != want {
			t.Fatalf("side %d segment send errors = %d, want %d", side, got, want)
		}
		if got, want := s.Stats().TCP.ChecksumErrors.Value(), uint64(0); got != want {
			t.Fatalf("side %d checksum errors = %d, want %d", side, got, want)
		}
	}

}
