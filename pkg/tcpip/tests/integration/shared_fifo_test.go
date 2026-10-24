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

	"gvisor.dev/gvisor/pkg/sync"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/waiter"
)

// TestVirtualTCPSharedFIFO is a short contention diagnostic, not a steady-state
// fairness or native-throughput benchmark. Both senders use one real NIC queue.
func TestVirtualTCPSharedFIFO(t *testing.T) {
	if got := runtime.GOMAXPROCS(0); got != 1 {
		t.Fatalf("GOMAXPROCS = %d, want 1", got)
	}
	for _, algorithm := range []string{"reno", "cubic"} {
		t.Run("single_"+algorithm, func(t *testing.T) {
			runSharedFIFO(t, []string{algorithm}, []int{0}, 0)
		})
	}
	for _, algorithms := range [][2]string{{"reno", "reno"}, {"cubic", "cubic"}, {"cubic", "reno"}, {"reno", "cubic"}} {
		for _, order := range [][2]int{{0, 1}, {1, 0}} {
			for _, stagger := range []time.Duration{0, time.Second} {
				mode := "simultaneous"
				if stagger != 0 {
					mode = "staggered"
				}
				name := fmt.Sprintf("%s_%s_order%d%d_%s", algorithms[0], algorithms[1], order[0], order[1], mode)
				t.Run(name, func(t *testing.T) { runSharedFIFO(t, algorithms[:], order[:], stagger) })
			}
		}
	}
}

type fifoSample struct {
	AtNS         int64
	Flow         int
	Accepted     int
	Delivered    int
	Highest      uint32
	Acknowledged uint32
	AppRemaining int
	Cwnd         uint32
	MSS          uint32
	Ssthresh     uint32
	RTTNS        int64
	RTONS        int64
	CCState      tcpip.CongestionControlState
	Probe        senderObservation
}

type fifoRecovery struct {
	AtNS      int64
	Flow      int
	Active    bool
	First     uint32
	Last      uint32
	SackedOut int
}

type fifoFlow struct {
	client       tcpip.Endpoint
	listener     tcpip.Endpoint
	server       tcpip.Endpoint
	data         []byte
	received     bytes.Buffer
	accepted     int
	dataSequence uint32
	highest      uint32
	acknowledged uint32
	connected    bool
	closed       bool
	eof          bool
	startAt      time.Duration
	startedNS    int64
	connectedNS  int64
	algorithm    string
}

type fifoFlowResult struct {
	Algorithm     string
	LocalPort     uint16
	RemotePort    uint16
	StartedNS     int64
	ConnectedNS   int64
	DataSequence  uint32
	Accepted      int
	Delivered     int
	Acknowledged  uint32
	EOF           bool
	FinWait2      bool
	PayloadSHA256 string
}

func runSharedFIFO(t *testing.T, algorithms []string, order []int, stagger time.Duration) {
	t.Helper()
	const quantum = time.Millisecond
	const rate = int64(1250000)
	const delay = 50 * time.Millisecond
	const capacity = 125000
	const windowStart = 2 * time.Second
	const windowEnd = 6 * time.Second
	const maxTime = 30 * time.Second
	clock := &completedClock{ManualClock: faketime.NewManualClock()}
	now := func() time.Duration { return clock.NowMonotonic().Sub(tcpip.MonotonicTime{}) }
	var stacks [2]*stack.Stack
	var links [2]*channel.Endpoint
	var pending []delivery
	flows := make([]fifoFlow, len(algorithms))
	var probeMu sync.Mutex
	probes := make([]senderObservation, len(flows))
	active := make([]bool, len(flows))
	var recoveries []fifoRecovery
	var negativeSACK bool
	protocol := tcp.NewProtocolProbe(func(state *tcp.TCPEndpointState) {
		index := int(state.ID.LocalPort) - 40000
		if index < 0 || index >= len(flows) || state.ID.RemotePort != uint16(8080+index) {
			return
		}
		row := senderObservation{AtNS: int64(state.SegTime.Sub(tcpip.MonotonicTime{})),
			Cwnd: state.Sender.SndCwnd, Outstanding: state.Sender.Outstanding, SackedOut: state.Sender.SackedOut,
			SndUna: uint32(state.Sender.SndUna), SndNxt: uint32(state.Sender.SndNxt),
			SndWnd: uint32(state.Sender.SndWnd), BufferedBytes: state.SndBufState.SndBufUsed}
		probeMu.Lock()
		defer probeMu.Unlock()
		probes[index] = row
		negativeSACK = negativeSACK || row.SackedOut < 0
		if active[index] != state.Sender.FastRecovery.Active {
			active[index] = state.Sender.FastRecovery.Active
			recoveries = append(recoveries, fifoRecovery{AtNS: row.AtNS, Flow: index, Active: active[index],
				First: uint32(state.Sender.FastRecovery.First), Last: uint32(state.Sender.FastRecovery.Last), SackedOut: row.SackedOut})
		}
	})
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
		s := stack.New(stack.Options{NetworkProtocols: []stack.NetworkProtocolFactory{ipv4.NewProtocol},
			TransportProtocols: []stack.TransportProtocolFactory{protocol}, Clock: clock, ClockResolution: quantum, SecureRNG: zeroReader{}})
		stacks[side] = s
		snd := tcpip.TCPSendBufferSizeRangeOption{Min: 4096, Default: 1 << 20, Max: 1 << 20}
		rcv := tcpip.TCPReceiveBufferSizeRangeOption{Min: 4096, Default: 1 << 20, Max: 1 << 20}
		if err := s.SetTransportProtocolOption(tcp.ProtocolNumber, &snd); err != nil {
			t.Fatal(err)
		}
		if err := s.SetTransportProtocolOption(tcp.ProtocolNumber, &rcv); err != nil {
			t.Fatal(err)
		}
		var sack tcpip.TCPSACKEnabled
		var recovery tcpip.TCPRecovery
		if err := s.TransportProtocolOption(tcp.ProtocolNumber, &sack); err != nil || !sack {
			t.Fatalf("SACK=%t, error=%v", sack, err)
		}
		if err := s.TransportProtocolOption(tcp.ProtocolNumber, &recovery); err != nil || recovery != tcpip.TCPRACKLossDetection {
			t.Fatalf("recovery=%d, error=%v", recovery, err)
		}
		links[side] = channel.New(16384, 1500, "")
		if err := s.CreateNIC(1, links[side]); err != nil {
			t.Fatal(err)
		}
		addr := tcpip.ProtocolAddress{Protocol: ipv4.ProtocolNumber, AddressWithPrefix: addresses[side].WithPrefix()}
		if err := s.AddProtocolAddress(1, addr, stack.AddressProperties{}); err != nil {
			t.Fatal(err)
		}
		s.SetRouteTable([]tcpip.Route{{Destination: header.IPv4EmptySubnet, NIC: 1}})
	}
	newEndpoint := func(side int) tcpip.Endpoint {
		var queue waiter.Queue
		ep, err := stacks[side].NewEndpoint(tcp.ProtocolNumber, ipv4.ProtocolNumber, &queue)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(ep.Close)
		return ep
	}
	for position, index := range order {
		flow := &flows[index]
		flow.algorithm = algorithms[index]
		flow.startAt = time.Duration(position) * stagger
		flow.startedNS = -1
		flow.connectedNS = -1
		flow.data = make([]byte, 16<<20)
		for i := range flow.data {
			flow.data[i] = byte(i*17 + i/251 + index*79)
		}
		flow.listener, flow.client = newEndpoint(1), newEndpoint(0)
		flow.listener.SocketOptions().SetReceiveBufferSize(1<<20, true)
		flow.client.SocketOptions().SetSendBufferSize(1<<20, true)
		cc := tcpip.CongestionControlOption(flow.algorithm)
		if err := flow.client.SetSockOpt(&cc); err != nil {
			t.Fatal(err)
		}
		if err := flow.listener.Bind(tcpip.FullAddress{Addr: addresses[1], Port: uint16(8080 + index)}); err != nil {
			t.Fatal(err)
		}
		if err := flow.listener.Listen(2); err != nil {
			t.Fatal(err)
		}
		if err := flow.client.Bind(tcpip.FullAddress{Addr: addresses[0], Port: uint16(40000 + index)}); err != nil {
			t.Fatal(err)
		}
	}
	// Events are [ordinal, captureNS, direction, flow, wireBytes, payloadBytes,
	// sequence, ack, serviceStartNS, serviceEndNS, arrivalNS, dropped, retransmitted].
	// Dropped packets are rejected before service. The capacity includes complete
	// bytes of an in-service packet until its end; propagation occupies no queue.
	var events [][13]int64
	var nextTX [2]time.Duration
	type queuedPacket struct {
		end   time.Duration
		bytes int
	}
	var queued []queuedPacket
	queuedBytes := 0
	var samples []fifoSample
	var syns [2][2]int
	capture := func() {
		for side, link := range links {
			for packet := link.Read(); packet != nil; packet = link.Read() {
				func() {
					defer packet.DecRef()
					view := packet.ToView()
					defer view.Release()
					ip := header.IPv4(view.AsSlice())
					if ip.TransportProtocol() != tcp.ProtocolNumber || ip.SourceAddress() != addresses[side] || ip.DestinationAddress() != addresses[1-side] {
						t.Fatal("unexpected packet identity")
					}
					segment := header.TCP(ip.Payload())
					index := int(segment.SourcePort()) - 40000
					if side == 1 {
						index = int(segment.DestinationPort()) - 40000
					}
					if index < 0 || index >= len(flows) {
						t.Fatalf("unknown flow: %d", index)
					}
					if side == 0 && segment.DestinationPort() != uint16(8080+index) || side == 1 && segment.SourcePort() != uint16(8080+index) {
						t.Fatal("wrong destination port")
					}
					flow := &flows[index]
					payload := segment.Payload()
					if segment.Flags().Contains(header.TCPFlagSyn) {
						options := header.ParseSynOptions(segment.Options(), segment.Flags().Contains(header.TCPFlagAck))
						if !options.SACKPermitted {
							t.Fatal("SACK not negotiated")
						}
						syns[index][side]++
						if side == 0 {
							flow.dataSequence = segment.SequenceNumber() + 1
						}
					}
					retx := int64(0)
					if side == 0 && len(payload) != 0 {
						offset := segment.SequenceNumber() - flow.dataSequence
						end := offset + uint32(len(payload))
						if end > uint32(flow.accepted) || !bytes.Equal(payload, flow.data[offset:end]) {
							t.Fatal("sender payload differs from this flow's accepted bytes")
						}
						if offset < flow.highest {
							retx = 1
						}
						flow.highest = max(flow.highest, end)
					}
					row := [13]int64{int64(len(events)), int64(now()), int64(side), int64(index), int64(packet.Size()), int64(len(payload)), int64(segment.SequenceNumber()), int64(segment.AckNumber()), -1, -1, -1, 0, retx}
					for len(queued) != 0 && queued[0].end <= now() {
						queuedBytes -= queued[0].bytes
						queued = queued[1:]
					}
					if side == 0 && queuedBytes+packet.Size() > capacity {
						row[11] = 1
					} else {
						start := max(nextTX[side], now())
						end := start + time.Duration((int64(packet.Size())*int64(time.Second)+rate-1)/rate)
						nextTX[side] = end
						row[8], row[9], row[10] = int64(start), int64(end), int64(end+delay)
						if side == 0 {
							queued = append(queued, queuedPacket{end, packet.Size()})
							queuedBytes += packet.Size()
						}
						pending = append(pending, delivery{at: end + delay, from: side, flow: index,
							packet: stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: packet.ToBuffer()}),
							row:    packetObservation{From: side, Acknowledgment: segment.AckNumber(), Flags: uint8(segment.Flags())}})
					}
					events = append(events, row)
					if len(events) > 50000 || len(pending) > 32768 {
						t.Fatal("bounded FIFO evidence exceeded")
					}
				}()
			}
		}
	}
	fence := func() { drainVirtualTCP(t, clock, stacks[:], capture) }
	sample := func() {
		probeMu.Lock()
		latest := append([]senderObservation(nil), probes...)
		probeMu.Unlock()
		for index := range flows {
			flow := &flows[index]
			if flow.server == nil {
				if now() >= windowStart {
					t.Fatalf("flow%d was not accepted before the common contention window", index)
				}
				continue
			}
			var info tcpip.TCPInfoOption
			if err := flow.client.GetSockOpt(&info); err != nil {
				t.Fatal(err)
			}
			samples = append(samples, fifoSample{AtNS: int64(now()), Flow: index, Accepted: flow.accepted, Delivered: flow.received.Len(),
				Highest: flow.highest, Acknowledged: flow.acknowledged, AppRemaining: len(flow.data) - flow.accepted,
				Cwnd: info.SndCwnd, MSS: info.SndMss, Ssthresh: info.SndSsthresh, RTTNS: int64(info.RTT), RTONS: int64(info.RTO), CCState: info.CcState, Probe: latest[index]})
			if now() >= windowStart && now() <= windowEnd && (flow.accepted == len(flow.data) || flow.accepted <= int(flow.acknowledged)) {
				t.Fatal("sender demand ended during contention interval")
			}
		}
	}
	defer func() {
		probeMu.Lock()
		changes := append([]fifoRecovery(nil), recoveries...)
		probeMu.Unlock()
		var results []fifoFlowResult
		for index := range flows {
			flow := &flows[index]
			results = append(results, fifoFlowResult{Algorithm: flow.algorithm, LocalPort: uint16(40000 + index), RemotePort: uint16(8080 + index),
				StartedNS: flow.startedNS, ConnectedNS: flow.connectedNS, DataSequence: flow.dataSequence, Accepted: flow.accepted,
				Delivered: flow.received.Len(), Acknowledged: flow.acknowledged, EOF: flow.eof,
				FinWait2: tcp.EndpointState(flow.client.State()) == tcp.StateFinWait2, PayloadSHA256: fmt.Sprintf("%x", sha256.Sum256(flow.received.Bytes()))})
		}
		result := struct {
			Case               string
			Algorithms         []string
			Order              []int
			StaggerNS          int64
			RateBytesPerSecond int64
			OneWayDelayNS      int64
			CapacityBytes      int
			WindowStartNS      int64
			WindowEndNS        int64
			VirtualEndNS       int64
			Flows              []fifoFlowResult
			Events             [][13]int64
			Samples            []fifoSample
			RecoveryChanges    []fifoRecovery
			RTOs               uint64
			SACKRecoveries     uint64
			TLPRecoveries      uint64
		}{t.Name(), algorithms, order, int64(stagger), rate, int64(delay), capacity, int64(windowStart), int64(windowEnd), int64(now()), results, events, samples, changes,
			stacks[0].Stats().TCP.Timeouts.Value(), stacks[0].Stats().TCP.SACKRecovery.Value(), stacks[0].Stats().TCP.TLPRecovery.Value()}
		encoded, err := json.Marshal(result)
		if err != nil {
			t.Fatal(err)
		}
		t.Logf("VIRTUAL_FIFO %s", encoded)
	}()
	nextSample := time.Duration(0)
	for {
		// Park the processors while applying each round of application demand.
		// Both nonblocking endpoint calls complete before the common fence.
		for _, s := range stacks {
			s.Pause()
		}
		func() {
			defer func() {
				for _, s := range stacks {
					s.Resume()
				}
			}()
			for _, index := range order {
				flow := &flows[index]
				if !flow.connected && now() >= flow.startAt {
					if _, ok := flow.client.Connect(tcpip.FullAddress{Addr: addresses[1], Port: uint16(8080 + index)}).(*tcpip.ErrConnectStarted); !ok {
						t.Fatal("connect did not start")
					}
					flow.connected = true
				}
				if flow.connected && flow.server == nil {
					ep, _, err := flow.listener.Accept(nil)
					if err == nil {
						flow.server = ep
						t.Cleanup(ep.Close)
						flow.connectedNS = int64(now())
					} else if _, ok := err.(*tcpip.ErrWouldBlock); !ok {
						t.Fatal(err)
					}
				}
				if flow.server == nil {
					continue
				}
				var cc tcpip.CongestionControlOption
				if err := flow.client.GetSockOpt(&cc); err != nil || string(cc) != flow.algorithm {
					t.Fatalf("flow%d algorithm=%q, error=%v", index, cc, err)
				}
				_, err := flow.server.Read(&flow.received, tcpip.ReadOptions{})
				switch err.(type) {
				case nil, *tcpip.ErrWouldBlock:
				case *tcpip.ErrClosedForReceive:
					flow.eof = true
				default:
					t.Fatal(err)
				}
				if now() < windowEnd {
					n, err := flow.client.Write(bytes.NewReader(flow.data[flow.accepted:]), tcpip.WriteOptions{})
					if _, blocked := err.(*tcpip.ErrWouldBlock); err != nil && !blocked {
						t.Fatal(err)
					}
					flow.accepted += int(n)
					if n != 0 && flow.startedNS < 0 {
						flow.startedNS = int64(now())
					}
				} else if !flow.closed {
					if err := flow.client.Shutdown(tcpip.ShutdownWrite); err != nil {
						t.Fatal(err)
					}
					flow.closed = true
				}
			}
		}()
		// Offer demand to both endpoints before observing their common NIC.
		fence()
		if now() == nextSample {
			sample()
			nextSample += 100 * time.Millisecond
		}
		complete := now() >= windowEnd && len(pending) == 0
		for index := range flows {
			flow := &flows[index]
			complete = complete && flow.eof && tcp.EndpointState(flow.client.State()) == tcp.StateFinWait2
		}
		if complete {
			break
		}
		if now() >= maxTime {
			t.Fatal("shared FIFO did not reach finite transfer completion")
		}
		next := min(now()+quantum, nextSample)
		for index := range flows {
			if !flows[index].connected {
				next = min(next, flows[index].startAt)
			}
		}
		selected := -1
		for index, packet := range pending {
			if packet.at < next || packet.at == next && selected == -1 {
				next, selected = packet.at, index
			}
		}
		advanceVirtualTCP(clock, stacks[:], next-now())
		fence()
		if selected != -1 {
			packet := pending[selected]
			pending = append(pending[:selected], pending[selected+1:]...)
			links[1-packet.from].InjectInbound(ipv4.ProtocolNumber, packet.packet)
			if packet.from == 1 && header.TCPFlags(packet.row.Flags).Contains(header.TCPFlagAck) {
				flow := &flows[packet.flow]
				flow.acknowledged = max(flow.acknowledged, min(flow.highest, packet.row.Acknowledgment-flow.dataSequence))
			}
			packet.packet.DecRef()
			fence()
		}
	}
	for index := range flows {
		flow := &flows[index]
		if !bytes.Equal(flow.received.Bytes(), flow.data[:flow.accepted]) || flow.acknowledged != uint32(flow.accepted) {
			t.Fatalf("flow%d payload/ACK differs", index)
		}
		if syns[index] != [2]int{1, 1} {
			t.Fatalf("flow%d SYN census=%v", index, syns[index])
		}
	}
	probeMu.Lock()
	badSACK := negativeSACK
	probeMu.Unlock()
	if badSACK {
		t.Fatal("negative native SackedOut observation")
	}
	for side, s := range stacks {
		if s.Stats().TCP.SegmentSendErrors.Value() != 0 || s.Stats().TCP.ChecksumErrors.Value() != 0 {
			t.Fatalf("side%d native link errors", side)
		}
	}
}
