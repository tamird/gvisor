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
	"testing/synctest"
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

// TestVirtualTCPSustainedFIFO compares one continuously backlogged sender per
// case after warmup. Tail drops arise from the declared finite queue; neither
// repeated loss nor an algorithm winner is a test success condition.
func TestVirtualTCPSustainedFIFO(t *testing.T) {
	if got, want := runtime.GOMAXPROCS(0), 1; got != want {
		t.Fatalf("GOMAXPROCS = %d, want %d", got, want)
	}
	for _, algorithm := range []string{"reno", "cubic"} {
		t.Run(algorithm, func(t *testing.T) {
			runSharedFIFOScenario(t, []string{algorithm}, []int{0}, 0, nil, fifoScenario{
				capacity: 31250, windowStart: 5 * time.Second, windowEnd: 20 * time.Second,
				maxTime: 60 * time.Second, dataBytes: 64 << 20, sustained: true,
			})
		})
	}
}

type fifoScenario struct {
	capacity    int
	windowStart time.Duration
	windowEnd   time.Duration
	maxTime     time.Duration
	dataBytes   int
	sustained   bool
}

type fifoSustainedSample struct {
	AtNS                                   int64
	QueuedBytes                            int
	Timeouts, Retransmits, FastRetransmits uint64
}

type fifoSustainedEvidence struct {
	OfferedBytes int
	PacketFlags  []uint16
	Measurements []fifoSustainedSample
}

const fifoDemandAt = 250 * time.Millisecond

// TestVirtualTCPRecoveryDiagnostic observes the existing mixed-flow pair without
// changing its packets, demand, clocks, or queue. The callback compares the full
// original observations to the retained first-write run before interpreting it.
func TestVirtualTCPRecoveryDiagnostic(t *testing.T) {
	if got := runtime.GOMAXPROCS(0); got != 1 {
		t.Fatalf("GOMAXPROCS = %d, want 1", got)
	}
	for _, write := range [][2]int{{0, 1}, {1, 0}} {
		t.Run(fmt.Sprintf("cubic_reno_setup01_write%d%d", write[0], write[1]), func(t *testing.T) {
			runSharedFIFOWithApplicationOrder(t, []string{"cubic", "reno"}, []int{0, 1}, 0,
				&fifoApplicationOrder{FirstWriteOrder: write[:], diagnostic: &fifoRecoveryDiagnostic{}})
		})
	}
}

// Diagnostic records cover only the Reno sender in the existing 0.6–6 s
// interval. Probe records are post-ACK; State records are read after each fence
// and retained when counters or congestion state change, including timer work.
type fifoRecoveryDiagnostic struct {
	Probes []fifoDiagnosticProbe
	State  []fifoDiagnosticState
	Wire   []fifoDiagnosticWire
}

type fifoDiagnosticProbe struct {
	AtNS, LastSendNS, RTONS, RACKXmitNS, RACKRTTNS, ReoWndNS int64
	Cwnd, Ssthresh, Outstanding, SackedOut, DupACKs, CAAcks  int
	SndUna, SndNxt, SndWnd, RACKEnd, FACK, MaxSACKED         uint32
	Reord, SpuriousRecovery                                  bool
	Recovery                                                 tcp.TCPFastRecoveryState
	SACK                                                     []header.SACKBlock
}

type fifoDiagnosticState struct {
	AtNS                                   int64
	Timeouts, Retransmits, FastRetransmits uint64
	Cwnd, Ssthresh                         uint32
	RTONS                                  int64
	CCState                                tcpip.CongestionControlState
}

type fifoDiagnosticWire struct {
	Event        int
	Flags        uint8
	Window       uint16
	TS           bool
	TSVal, TSEcr uint32
	SACK         []header.SACKBlock
}

type fifoFirstWrite struct {
	Flow     int
	AtNS     int64
	Accepted int
}

type fifoApplicationOrder struct {
	diagnostic         *fifoRecoveryDiagnostic
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
	runSharedFIFOScenario(t, algorithms, order, stagger, application, fifoScenario{
		capacity: 125000, windowStart: 2 * time.Second, windowEnd: 6 * time.Second,
		maxTime: 30 * time.Second, dataBytes: 16 << 20,
	})
}

func runSharedFIFOScenario(t *testing.T, algorithms []string, order []int, stagger time.Duration, application *fifoApplicationOrder, scenario fifoScenario) {
	clock := &completedClock{ManualClock: faketime.NewManualClock()}
	runFIFOWithClock(t, algorithms, order, stagger, application, scenario, clock,
		func(stacks []*stack.Stack, observe func()) { drainVirtualTCP(t, clock, stacks, observe) },
		func(stacks []*stack.Stack, d time.Duration) { advanceVirtualTCP(clock, stacks, d) })
}

// Compare the two clocks on the same FIFO, application and pair of TCP stacks.
// Keep the packet observations from each driver even if their schedules differ.
func TestVirtualTCPClockComparison(t *testing.T) {
	scenario := fifoScenario{capacity: 125000, windowStart: 2 * time.Second, windowEnd: 6 * time.Second,
		maxTime: 30 * time.Second, dataBytes: 16 << 20}
	algorithms, order := []string{"cubic", "reno"}, []int{0, 1}
	for _, write := range [][2]int{{0, 1}, {1, 0}} {
		t.Run(fmt.Sprintf("write%d%d", write[0], write[1]), func(t *testing.T) {
			t.Run("manual", func(t *testing.T) {
				runSharedFIFOScenario(t, algorithms, order, 0, &fifoApplicationOrder{FirstWriteOrder: write[:]}, scenario)
			})
			t.Run("synctest", func(t *testing.T) {
				synctest.Test(t, func(t *testing.T) {
					park := func(stacks []*stack.Stack, f func()) {
						for _, s := range stacks {
							s.Pause()
						}
						defer func() {
							for _, s := range stacks {
								s.Resume()
							}
						}()
						synctest.Wait()
						f()
					}
					drain := func(stacks []*stack.Stack, observe func()) {
						before := time.Now()
						park(stacks, observe)
						if !time.Now().Equal(before) {
							t.Fatal("clock advanced while draining TCP work")
						}
					}
					advance := func(stacks []*stack.Stack, d time.Duration) {
						before := time.Now()
						park(stacks, func() {
							time.Sleep(d)
							synctest.Wait()
						})
						if got := time.Since(before); got != d {
							t.Fatalf("clock advanced %s, want %s", got, d)
						}
					}
					runFIFOWithClock(t, algorithms, order, 0, &fifoApplicationOrder{FirstWriteOrder: write[:]},
						scenario, tcpip.NewStdClock(), drain, advance)
				})
			})
		})
	}
}

func runFIFOWithClock(t *testing.T, algorithms []string, order []int, stagger time.Duration, application *fifoApplicationOrder, scenario fifoScenario,
	clock tcpip.Clock, drain func([]*stack.Stack, func()), advance func([]*stack.Stack, time.Duration)) {
	t.Helper()
	const quantum = time.Millisecond
	const rate = int64(1250000)
	const delay = 50 * time.Millisecond
	capacity, windowStart, windowEnd, maxTime := scenario.capacity, scenario.windowStart, scenario.windowEnd, scenario.maxTime
	var sustained *fifoSustainedEvidence
	if scenario.sustained {
		if got, want := len(algorithms), 1; got != want {
			t.Fatalf("sustained flow count = %d, want %d", got, want)
		}
		sustained = &fifoSustainedEvidence{OfferedBytes: scenario.dataBytes}
	}
	var diagnostic *fifoRecoveryDiagnostic
	if application != nil {
		diagnostic = application.diagnostic
	}
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
		if diagnostic != nil && index == 1 && row.AtNS >= int64(600*time.Millisecond) && row.AtNS <= int64(windowEnd) {
			s := state.Sender
			diagnostic.Probes = append(diagnostic.Probes, fifoDiagnosticProbe{
				AtNS: row.AtNS, LastSendNS: int64(s.LastSendTime.Sub(tcpip.MonotonicTime{})), RTONS: int64(s.RTO),
				RACKXmitNS: int64(s.RACKState.XmitTime.Sub(tcpip.MonotonicTime{})), RACKRTTNS: int64(s.RACKState.RTT), ReoWndNS: int64(s.RACKState.ReoWnd),
				Cwnd: s.SndCwnd, Ssthresh: s.Ssthresh, Outstanding: s.Outstanding, SackedOut: s.SackedOut, DupACKs: s.DupAckCount, CAAcks: s.SndCAAckCount,
				SndUna: uint32(s.SndUna), SndNxt: uint32(s.SndNxt), SndWnd: uint32(s.SndWnd), RACKEnd: uint32(s.RACKState.EndSequence), FACK: uint32(s.RACKState.FACK),
				MaxSACKED: uint32(state.SACK.MaxSACKED), Reord: s.RACKState.Reord, SpuriousRecovery: s.SpuriousRecovery,
				Recovery: s.FastRecovery, SACK: append([]header.SACKBlock(nil), state.SACK.ReceivedBlocks...),
			})
		}
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
		flow.data = make([]byte, scenario.dataBytes)
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
					if diagnostic != nil && index == 1 && now() >= 600*time.Millisecond && now() <= windowEnd {
						options := header.ParseTCPOptions(segment.Options())
						diagnostic.Wire = append(diagnostic.Wire, fifoDiagnosticWire{Event: len(events), Flags: uint8(segment.Flags()), Window: segment.WindowSize(),
							TS: options.TS, TSVal: options.TSVal, TSEcr: options.TSEcr, SACK: append([]header.SACKBlock(nil), options.SACKBlocks...)})
					}
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
					if sustained != nil {
						sustained.PacketFlags = append(sustained.PacketFlags, uint16(segment.Flags()))
					}
					if len(events) > 50000 || len(pending) > 32768 {
						t.Fatal("bounded FIFO evidence exceeded")
					}
				}()
			}
		}
	}
	fence := func() {
		drain(stacks[:], capture)
		if diagnostic == nil || now() < 600*time.Millisecond || now() > windowEnd {
			return
		}
		endpoint := flows[1].client
		stats := endpoint.Stats().(*tcp.Stats)
		var info tcpip.TCPInfoOption
		if err := endpoint.GetSockOpt(&info); err != nil {
			t.Fatal(err)
		}
		row := fifoDiagnosticState{Timeouts: stats.SendErrors.Timeouts.Value(), Retransmits: stats.SendErrors.Retransmits.Value(), FastRetransmits: stats.SendErrors.FastRetransmit.Value(),
			Cwnd: info.SndCwnd, Ssthresh: info.SndSsthresh, RTONS: int64(info.RTO), CCState: info.CcState}
		probeMu.Lock()
		defer probeMu.Unlock()
		if len(diagnostic.Probes) > 10000 || len(diagnostic.State) > 10000 || len(diagnostic.Wire) > 10000 {
			t.Fatal("bounded recovery diagnostic exceeded")
		}
		if len(diagnostic.State) != 0 {
			previous := diagnostic.State[len(diagnostic.State)-1]
			previous.AtNS = 0
			if previous == row {
				return
			}
		}
		row.AtNS = int64(now())
		diagnostic.State = append(diagnostic.State, row)
	}
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
			if sustained != nil {
				var occupancy int
				for _, packet := range queued {
					if packet.end > now() {
						occupancy += packet.bytes
					}
				}
				stats := flow.client.Stats().(*tcp.Stats)
				sustained.Measurements = append(sustained.Measurements, fifoSustainedSample{
					AtNS: int64(now()), QueuedBytes: occupancy, Timeouts: stats.SendErrors.Timeouts.Value(),
					Retransmits: stats.SendErrors.Retransmits.Value(), FastRetransmits: stats.SendErrors.FastRetransmit.Value(),
				})
			}
			if now() >= windowStart && now() <= windowEnd && (flow.accepted == len(flow.data) || flow.accepted <= int(flow.acknowledged)) {
				t.Fatal("sender demand ended during contention interval")
			}
		}
	}
	defer func() {
		probeMu.Lock()
		changes := append([]fifoRecovery(nil), recoveries...)
		var diagnosticCopy *fifoRecoveryDiagnostic
		if diagnostic != nil {
			diagnosticCopy = &fifoRecoveryDiagnostic{
				Probes: append([]fifoDiagnosticProbe(nil), diagnostic.Probes...),
				State:  append([]fifoDiagnosticState(nil), diagnostic.State...),
				Wire:   append([]fifoDiagnosticWire(nil), diagnostic.Wire...),
			}
		}
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
			ApplicationOrder   *fifoApplicationOrder   `json:",omitempty"`
			Diagnostic         *fifoRecoveryDiagnostic `json:",omitempty"`
			Sustained          *fifoSustainedEvidence  `json:",omitempty"`
		}{t.Name(), algorithms, order, int64(stagger), rate, int64(delay), capacity, int64(windowStart), int64(windowEnd), int64(now()), results, events, samples, changes,
			stacks[0].Stats().TCP.Timeouts.Value(), stacks[0].Stats().TCP.SACKRecovery.Value(), stacks[0].Stats().TCP.TLPRecovery.Value(), application, diagnosticCopy, sustained}
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
				for _, index := range application.FirstWriteOrder {
					writeFlow(index)
				}
				demandStarted = true
			}
		}()
		// Offer demand to both endpoints before observing their common NIC.
		fence()
		sampleReady := now() == nextSample
		if sustained != nil && sampleReady {
			for _, packet := range pending {
				if packet.at <= now() {
					sampleReady = false
				}
			}
		}
		if sampleReady {
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
		advance(stacks[:], next-now())
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
