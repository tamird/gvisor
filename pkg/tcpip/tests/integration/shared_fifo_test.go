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

// TestVirtualTCPApplicationOrder changes only the first Write batch after both
// connections have established. Later application batches use the setup order.
func TestVirtualTCPApplicationOrder(t *testing.T) {
	if got := runtime.GOMAXPROCS(0); got != 1 {
		t.Fatalf("GOMAXPROCS = %d, want 1", got)
	}
	for _, algorithms := range [][2]string{{"cubic", "reno"}, {"reno", "reno"}, {"cubic", "cubic"}} {
		for _, setup := range [][2]int{{0, 1}, {1, 0}} {
			if algorithms[0] == algorithms[1] && setup[0] == 1 {
				continue
			}
			for _, firstWrite := range [][2]int{{0, 1}, {1, 0}} {
				name := fmt.Sprintf("%s_%s_setup%d%d_write%d%d", algorithms[0], algorithms[1], setup[0], setup[1], firstWrite[0], firstWrite[1])
				t.Run(name, func(t *testing.T) {
					runSharedFIFOWithApplicationOrder(t, algorithms[:], setup[:], 0, &fifoApplicationOrder{FirstWriteOrder: firstWrite[:]})
				})
			}
		}
	}
}

const fifoDemandAt = 250 * time.Millisecond

// TestVirtualTCPFirstCommonLoss keeps the two senders continuously backlogged
// until a real covering ACK reaches the original sender, rather than timing the whole transfer.
func TestVirtualTCPFirstCommonLoss(t *testing.T) {
	if runtime.GOMAXPROCS(0) != 1 {
		t.Fatal("requires the original single-CPU deterministic schedule")
	}
	for _, scenario := range []string{"original", "without_hystart", "one_more_frame"} {
		t.Run(scenario, func(t *testing.T) {
			observation := firstLossObservation{Scenario: scenario}
			app := fifoApplicationOrder{FirstWriteOrder: []int{0, 1}, firstLoss: &observation}
			runSharedFIFOWithApplicationOrder(t, []string{"cubic", "reno"}, []int{0, 1}, 0, &app)
		})
	}
}

func TestVirtualTCPRecoveryDecision(t *testing.T) {
	if runtime.GOMAXPROCS(0) != 1 {
		t.Fatal("requires the original single processor")
	}
	modes := []string{"original"}
	if tcp.FIFOTraceCanRestore {
		modes = append(modes, "old_entry_gate")
	}
	for _, mode := range modes {
		t.Run(mode, func(t *testing.T) {
			app := fifoApplicationOrder{FirstWriteOrder: []int{0, 1}, recoveryMode: mode}
			runSharedFIFOWithApplicationOrder(t, []string{"cubic", "reno"}, []int{0, 1}, 0, &app)
		})
	}
}

// TestVirtualTCPFirstRTOEvidence reuses three real CUBIC service slots.
// Both arms suppress the same three CUBIC frames. The replacement bytes are
// original Reno packets from this run, never synthetic ACKs or sender sends.
func TestVirtualTCPFirstRTOEvidence(t *testing.T) {
	if runtime.GOMAXPROCS(0) != 1 {
		t.Fatal("requires the original single processor")
	}
	for _, mode := range []string{"duplicate", "new_sack"} {
		t.Run(mode, func(t *testing.T) {
			app := fifoApplicationOrder{FirstWriteOrder: []int{0, 1}, recoveryMode: mode, substitute: &slotEvidence{}}
			runSharedFIFOWithApplicationOrder(t, []string{"cubic", "reno"}, []int{0, 1}, 0, &app)
		})
	}
}

const slotHead uint32 = 447300
const slotMSS = 1420
const slotBytes = 1472

// End both treatments at the exact already-measured corrected baseline horizon.
const slotEnd = 3773123200 * time.Nanosecond

type slotRecord struct {
	Index, DonorIndex               int
	DonorOffset, CUBICOffset        uint32
	Capture, Start, Finish, Arrival int64
	BeforeBytes, Capacity           int
	Receiver                        int
	Sender                          tcp.FirstLossSender
	TS, Echo                        uint32
	Digest                          string
}

type slotACK struct {
	Flow                  int
	Capture, Arrival      int64
	ACK, Window, TS, Echo uint32
	SACK                  [][2]uint32
	Before, After         tcp.FirstLossSender
}

type slotEvidence struct {
	Slots []slotRecord
	ACKs  []slotACK
}

type firstLossEvent struct {
	Row                   [13]int64
	Offset, End           uint32
	QueueBefore, Capacity int
	Timeouts              uint64
	Senders               [2]tcp.FirstLossSender
}

type firstLossObservation struct {
	Scenario                                   string
	BaselineFull, Dropped, Repair, CoveringACK *firstLossEvent
	CoveringACKReceivedNS                      int64
	SenderAfterACK                             tcp.FirstLossSender
	SenderAcknowledged                         uint32
}

type fifoFirstWrite struct {
	Flow     int
	AtNS     int64
	Accepted int
}

type fifoApplicationOrder struct {
	recoveryMode string
	substitute   *slotEvidence
	firstLoss    *firstLossObservation

	FirstWriteOrder    []int
	DemandAtNS         int64
	PreGatePackets     []string
	PreGateEventCount  int
	PreGateConnectedNS []int64
	PreGateSenders     []fifoSample
	PreGatePending     int
	PreGateQueuedBytes int
	PreGateNextTXNS    [2]int64
	Writes             []fifoFirstWrite
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
	runSharedFIFOWithApplicationOrder(t, algorithms, order, stagger, nil)
}

func runSharedFIFOWithApplicationOrder(t *testing.T, algorithms []string, order []int, stagger time.Duration, application *fifoApplicationOrder) {
	t.Helper()
	const quantum = time.Millisecond
	const rate = int64(1250000)
	const delay = 50 * time.Millisecond
	const capacity = 125000
	const windowStart = 2 * time.Second
	windowEnd := 6 * time.Second
	var diagnostic *firstLossObservation
	limit := capacity
	if application != nil && application.firstLoss != nil {
		diagnostic = application.firstLoss
		windowEnd = 20 * time.Second
		if diagnostic.Scenario == "one_more_frame" {
			limit += 1472
		}
	}
	tracing := application != nil && application.recoveryMode != ""
	if tracing {
		windowEnd = 20 * time.Second
	}
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
	var intervention *slotEvidence
	if application != nil {
		intervention = application.substitute
	}
	type originalPacket struct {
		packet   *stack.PacketBuffer
		index    int
		ts, echo uint32
		digest   string
	}
	retained := make(map[uint32]originalPacket)
	defer func() {
		for _, p := range retained {
			p.packet.DecRef()
		}
	}()
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
					if application != nil && now() < fifoDemandAt {
						application.PreGatePackets = append(application.PreGatePackets, fmt.Sprintf("%x", view.AsSlice()))
					}
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
					before := queuedBytes
					if intervention != nil && side == 0 && index == 1 && len(payload) == slotMSS && retx == 0 {
						offset := segment.SequenceNumber() - flow.dataSequence
						if offset >= slotHead-3*slotMSS && offset < slotHead+4*slotMSS && offset != slotHead && (offset-(slotHead-3*slotMSS))%slotMSS == 0 {
							if _, ok := retained[offset]; ok || packet.Size() != slotBytes {
								t.Fatalf("invalid donor offset %d", offset)
							}
							opts := header.ParseTCPOptions(segment.Options())
							if !opts.TS {
								t.Fatal("expected negotiated timestamps")
							}
							retained[offset] = originalPacket{stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: packet.ToBuffer()}), len(events), opts.TSVal, opts.TSEcr, fmt.Sprintf("%x", sha256.Sum256(view.AsSlice()))}
						}
					}
					if side == 0 && queuedBytes+packet.Size() > limit {
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
						onWire, deliveredFlow, flags, ack := packet, index, segment.Flags(), segment.AckNumber()
						if intervention != nil && side == 0 && index == 0 && len(payload) == slotMSS && now() > 1455616000*time.Nanosecond && len(intervention.Slots) < 3 {
							n := len(intervention.Slots)
							offset := slotHead + uint32(n+1)*slotMSS
							if application.recoveryMode == "duplicate" {
								offset = slotHead - uint32(3-n)*slotMSS
							}
							donor, ok := retained[offset]
							if !ok || packet.Size() != slotBytes || donor.packet.Size() != slotBytes || retx != 0 {
								t.Fatal("missing original equal-size packet or unexpected service slot")
							}
							reno := &flows[1]
							sender := reno.client.(*tcp.Endpoint).FirstLossSender()
							if sender.SndUna-reno.dataSequence != slotHead || sender.SackedOut != 1 || !sender.Recovery || !sender.TimerArmed || sender.TimerTargetNS != 1772693134 || reno.received.Len() != int(slotHead) {
								t.Fatalf("unexpected pre-intervention Reno state: %+v delivered=%d", sender, reno.received.Len())
							}
							intervention.Slots = append(intervention.Slots, slotRecord{len(events), donor.index, offset, segment.SequenceNumber() - flow.dataSequence, int64(now()), int64(start), int64(end), int64(end + delay), before, limit, reno.received.Len(), sender, donor.ts, donor.echo, donor.digest})
							onWire, deliveredFlow = donor.packet, 1
							v := onWire.ToView()
							h := header.TCP(header.IPv4(v.AsSlice()).Payload())
							if h.SourcePort() != 40001 || h.DestinationPort() != 8081 || !bytes.Equal(h.Payload(), reno.data[offset:offset+slotMSS]) || fmt.Sprintf("%x", sha256.Sum256(v.AsSlice())) != donor.digest {
								t.Fatal("retained packet identity mismatch")
							}
							flags, ack = h.Flags(), h.AckNumber()
							v.Release()
						}
						pending = append(pending, delivery{at: end + delay, from: side, flow: deliveredFlow,
							packet: stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: onWire.ToBuffer()}),
							row:    packetObservation{From: side, CapturedAtNS: int64(now()), Acknowledgment: ack, Flags: uint8(flags)}})
					}
					if diagnostic != nil && now() >= fifoDemandAt {
						observe := func() *firstLossEvent {
							offset := segment.SequenceNumber() - flow.dataSequence
							if side == 1 {
								offset = segment.AckNumber() - flow.dataSequence
							}
							ev := firstLossEvent{Row: row, Offset: offset, End: offset + uint32(len(payload)), QueueBefore: before, Capacity: limit, Timeouts: stacks[0].Stats().TCP.Timeouts.Value()}
							for i := range flows {
								ev.Senders[i] = flows[i].client.(*tcp.Endpoint).FirstLossSender()
							}
							return &ev
						}
						if side == 0 && len(payload) > 0 {
							if diagnostic.BaselineFull == nil && before+packet.Size() > capacity {
								diagnostic.BaselineFull = observe()
							}
							if diagnostic.Dropped == nil && row[11] == 1 {
								diagnostic.Dropped = observe()
							}
							if d := diagnostic.Dropped; d != nil && diagnostic.Repair == nil && int(d.Row[3]) == index && retx == 1 && row[11] == 0 {
								start := segment.SequenceNumber() - flow.dataSequence
								if start <= d.Offset && start+uint32(len(payload)) >= d.End {
									diagnostic.Repair = observe()
								}
							}
						}
						if d := diagnostic.Dropped; d != nil && side == 1 && index == int(d.Row[3]) && diagnostic.Repair != nil && diagnostic.CoveringACK == nil && segment.Flags().Contains(header.TCPFlagAck) && segment.AckNumber()-flow.dataSequence >= d.End {
							diagnostic.CoveringACK = observe()
						}
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
		if tracing || diagnostic != nil {
			return
		}
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
			ApplicationOrder   *fifoApplicationOrder `json:",omitempty"`
		}{t.Name(), algorithms, order, int64(stagger), rate, int64(delay), capacity, int64(windowStart), int64(windowEnd), int64(now()), results, events, samples, changes,
			stacks[0].Stats().TCP.Timeouts.Value(), stacks[0].Stats().TCP.SACKRecovery.Value(), stacks[0].Stats().TCP.TLPRecovery.Value(), application}
		encoded, err := json.Marshal(result)
		if err != nil {
			t.Fatal(err)
		}
		t.Logf("VIRTUAL_FIFO %s", encoded)
	}()
	writeFlow := func(index int) {
		flow := &flows[index]
		n, err := flow.client.Write(bytes.NewReader(flow.data[flow.accepted:]), tcpip.WriteOptions{})
		if _, blocked := err.(*tcpip.ErrWouldBlock); err != nil && !blocked {
			t.Fatal(err)
		}
		flow.accepted += int(n)
		if n != 0 && flow.startedNS < 0 {
			flow.startedNS = int64(now())
		}
		if application != nil && len(application.Writes) < len(flows) {
			if n == 0 || err != nil || now() != fifoDemandAt {
				t.Fatalf("first write flow%d accepted=%d error=%v at=%s", index, n, err, now())
			}
			application.Writes = append(application.Writes, fifoFirstWrite{Flow: index, AtNS: int64(now()), Accepted: int(n)})
		}
	}
	demandStarted := application == nil
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
				if !demandStarted {
					continue
				}
				if now() < windowEnd {
					writeFlow(index)
				} else if !flow.closed {
					if err := flow.client.Shutdown(tcpip.ShutdownWrite); err != nil {
						t.Fatal(err)
					}
					flow.closed = true
				}
			}
			if !demandStarted && now() >= fifoDemandAt {
				if now() != fifoDemandAt || len(pending) != 0 {
					t.Fatalf("demand gate at=%s pending=%d", now(), len(pending))
				}
				application.DemandAtNS = int64(now())
				application.PreGateEventCount = len(events)
				application.PreGatePending = len(pending)
				application.PreGateNextTXNS = [2]int64{int64(nextTX[0]), int64(nextTX[1])}
				for _, packet := range queued {
					if packet.end > now() {
						application.PreGateQueuedBytes += packet.bytes
					}
				}
				if application.PreGateQueuedBytes != 0 {
					t.Fatal("handshake FIFO has not drained before demand")
				}
				probeMu.Lock()
				latest := append([]senderObservation(nil), probes...)
				probeMu.Unlock()
				for index := range flows {
					flow := &flows[index]
					if flow.server == nil || flow.accepted != 0 || flow.received.Len() != 0 || tcp.EndpointState(flow.client.State()) != tcp.StateEstablished || tcp.EndpointState(flow.server.State()) != tcp.StateEstablished {
						t.Fatalf("flow%d is not established and empty before demand", index)
					}
					var info tcpip.TCPInfoOption
					if err := flow.client.GetSockOpt(&info); err != nil {
						t.Fatal(err)
					}
					application.PreGateConnectedNS = append(application.PreGateConnectedNS, flow.connectedNS)
					application.PreGateSenders = append(application.PreGateSenders, fifoSample{AtNS: int64(now()), Flow: index,
						AppRemaining: len(flow.data), Cwnd: info.SndCwnd, MSS: info.SndMss, Ssthresh: info.SndSsthresh,
						RTTNS: int64(info.RTT), RTONS: int64(info.RTO), CCState: info.CcState, Probe: latest[index]})
				}
				if tracing {
					for i := range flows {
						flows[i].client.(*tcp.Endpoint).EnableFIFOTrace(flows[i].dataSequence, application.recoveryMode == "old_entry_gate")
					}
				}
				if diagnostic != nil && diagnostic.Scenario == "without_hystart" {
					flows[0].client.(*tcp.Endpoint).FirstLossDisableHyStart()
				}
				for _, index := range application.FirstWriteOrder {
					writeFlow(index)
				}
				demandStarted = true
			}
		}()
		// Offer demand to both endpoints before observing their common NIC.
		fence()
		if now() == nextSample {
			sample()
			nextSample += 100 * time.Millisecond
		}
		if diagnostic != nil {
			if r := diagnostic.CoveringACK; r != nil && int64(now()) >= r.Row[10] && flows[int(r.Row[3])].acknowledged >= diagnostic.Dropped.End {
				diagnostic.CoveringACKReceivedNS = int64(now())
				owner := &flows[int(r.Row[3])]
				diagnostic.SenderAfterACK = owner.client.(*tcp.Endpoint).FirstLossSender()
				diagnostic.SenderAcknowledged = diagnostic.SenderAfterACK.SndUna - owner.dataSequence
				if diagnostic.SenderAcknowledged < diagnostic.Dropped.End || diagnostic.SenderAcknowledged > uint32(owner.accepted) {
					t.Fatalf("sender failed to advance through missing bytes: %+v", diagnostic)
				}
				if diagnostic.BaselineFull == nil || diagnostic.Dropped.Row[12] != 0 || diagnostic.Dropped.QueueBefore+int(diagnostic.Dropped.Row[4]) <= limit || r.Row[10] != int64(now()) {
					t.Fatalf("inconsistent first-drop or ACK: %+v", diagnostic)
				}
				for i := range flows {
					if flows[i].accepted >= len(flows[i].data) || flows[i].accepted <= flows[i].received.Len() {
						t.Fatalf("flow %d lacks continuous demand", i)
					}
				}
				encoded, err := json.Marshal(diagnostic)
				if err != nil {
					t.Fatal(err)
				}
				t.Logf("FIRST_LOSS %s", encoded)
				return
			}
			if now() > 10*time.Second {
				t.Fatalf("no first repair completed in the bounded prefix: %+v", diagnostic)
			}
		}
		if tracing && demandStarted {
			first := int64(0)
			for i := range flows {
				v := flows[i].client.(*tcp.Endpoint).FIFOTraceFirst()
				if v != 0 && (first == 0 || v < first) {
					first = v
				}
			}
			done := (first != 0 && int64(now()) >= first+int64(2*time.Second)) || now() >= 20*time.Second
			if intervention != nil {
				done = now() == slotEnd
			}
			if done {
				type flowEnd struct {
					Base                uint32
					Accepted, Delivered int
					ACK                 uint32
					PayloadSHA          string
					Sender              tcp.FirstLossSender
					Decisions           []tcp.FIFOGrant
				}
				out := struct {
					Mode         string
					End, First   int64
					Flows        []flowEnd
					Packets      int
					Intervention *slotEvidence
				}{Mode: application.recoveryMode, End: int64(now()), First: first, Packets: len(events), Intervention: intervention}
				for i := range flows {
					f := &flows[i]
					ep := f.client.(*tcp.Endpoint)
					received := f.received.Bytes()
					if !bytes.Equal(received, f.data[:len(received)]) || f.accepted <= len(received) {
						t.Fatal("wrong received stream or demand ended")
					}
					out.Flows = append(out.Flows, flowEnd{f.dataSequence, f.accepted, len(received), f.acknowledged, fmt.Sprintf("%x", sha256.Sum256(received)), ep.FirstLossSender(), ep.FIFOTraceEvents()})
				}
				if intervention != nil && (len(intervention.Slots) != 3 || len(intervention.ACKs) == 0) {
					t.Fatal("missing intervention evidence")
				}
				if intervention != nil {
					for start := 0; start < len(intervention.ACKs); start += 16 {
						v, err := json.Marshal(intervention.ACKs[start:min(start+16, len(intervention.ACKs))])
						if err != nil {
							t.Fatal(err)
						}
						t.Logf("FIFO_ACKS %s %s", out.Mode, v)
					}
					intervention.ACKs = nil
					for _, s := range stacks {
						if s.Stats().TCP.ChecksumErrors.Value() != 0 || s.Stats().TCP.SegmentSendErrors.Value() != 0 {
							t.Fatal("link integrity error")
						}
					}
				}
				for start := 0; start < len(events); start += 64 {
					e := events[start:min(start+64, len(events))]
					v, err := json.Marshal(e)
					if err != nil {
						t.Fatal(err)
					}
					t.Logf("FIFO_WIRE %s %s", out.Mode, v)
				}
				v, err := json.Marshal(out)
				if err != nil {
					t.Fatal(err)
				}
				t.Logf("FIFO_DECISIONS %s", v)
				return
			}
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
		if intervention != nil {
			next = min(next, slotEnd)
		}
		if !demandStarted {
			next = min(next, fifoDemandAt)
		}
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
			var observation *slotACK
			if intervention != nil && packet.from == 1 && now() >= 1400*time.Millisecond && now() <= 2100*time.Millisecond {
				v := packet.packet.ToView()
				h := header.TCP(header.IPv4(v.AsSlice()).Payload())
				f := &flows[packet.flow]
				opts := header.ParseTCPOptions(h.Options())
				a := slotACK{Flow: packet.flow, Capture: packet.row.CapturedAtNS, Arrival: int64(now()), ACK: h.AckNumber() - f.dataSequence, Window: uint32(h.WindowSize()), TS: opts.TSVal, Echo: opts.TSEcr, Before: f.client.(*tcp.Endpoint).FirstLossSender()}
				for _, b := range opts.SACKBlocks {
					a.SACK = append(a.SACK, [2]uint32{uint32(b.Start) - f.dataSequence, uint32(b.End) - f.dataSequence})
				}
				v.Release()
				observation = &a
			}
			links[1-packet.from].InjectInbound(ipv4.ProtocolNumber, packet.packet)
			if packet.from == 1 && header.TCPFlags(packet.row.Flags).Contains(header.TCPFlagAck) {
				flow := &flows[packet.flow]
				flow.acknowledged = max(flow.acknowledged, min(flow.highest, packet.row.Acknowledgment-flow.dataSequence))
			}
			packet.packet.DecRef()
			fence()
			if observation != nil {
				observation.After = flows[packet.flow].client.(*tcp.Endpoint).FirstLossSender()
				intervention.ACKs = append(intervention.ACKs, *observation)
			}
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
