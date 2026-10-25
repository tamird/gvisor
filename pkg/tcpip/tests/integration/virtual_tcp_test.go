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
	NominalAtNS    int64  `json:",omitempty"`
	TSEcr          uint32 `json:",omitempty"`
	WireWindow     uint16 `json:",omitempty"`
	WireTSVal      uint32 `json:",omitempty"`
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
					runVirtualTCP(t, algorithm.name, algorithm.protocol, virtualScenario{
						delay: 10 * time.Millisecond, rate: 2500000,
						maxTime: 10 * time.Second, bytes: 128 << 10,
						dropOffset: loss.offset, burstSegments: 1,
					})
				})
			}
		})
	}
}

type virtualScenario struct {
	delay         time.Duration
	rate          int64
	maxTime       time.Duration
	bytes         int
	dropOffset    int
	burstSegments int
	streaming     bool
	startup       *startupPulse
	hyStartPlus   bool
	forwardQueue  *startupQueue
	flight        *flightRecorder
}

// Capture both 40-MSS pulses and the following flight, so the timestamp
// echoes can establish RTT relief.
const startupTraceMSS = 160

type startupPulse struct {
	DelayNS                       time.Duration
	LowerMSS                      uint32
	MSS                           uint32
	DataSequence                  uint32
	FirstPayloadNS                int64
	ACKs                          []startupACK
	Deadline                      *streamRound
	Transitions                   []startupTransition
	PeakCwnd, FirstSecondPeakCwnd int
}

type startupACK struct {
	AtNS, RoundStartNS, LastAckNS int64
	SndUna, EndSeq, TSNow         uint32
	Cwnd, Ssthresh                int
	LastRTT, CurrRTT              time.Duration
	SampleCount                   uint
	SndNxt                        uint32
	Plus, CSS                     bool
	CSSRounds                     uint
	CSSBaseline, ActualRTT        time.Duration
	Credit                        int
	EpochNS                       int64
	WMax, WEst                    float64
}

type startupTransition struct {
	Before, After      startupACK
	WireAtNS           int64
	WireACK, WireTSEcr uint32
}

// A byte-capacity FIFO on the forward wire. Backlog is residual service
// (including any partially transmitted packet), never propagation time.
type startupQueue struct {
	Rate, Capacity                                  int64
	OfferedPackets, AdmittedPackets, DroppedPackets int
	OfferedBytes, AdmittedBytes, DroppedBytes       int64
	PeakBacklogNS, Above25msNS, LongestAbove25msNS  time.Duration
	FirstSecondAbove25msNS, FirstSecondPeakNS       time.Duration
	FirstSecondDrops                                int
	last, streak                                    time.Duration
}

func (q *startupQueue) account(now, serviceEnd, first time.Duration) {
	// Between admissions the residual service drains linearly.
	stop := min(now, serviceEnd-25*time.Millisecond)
	above := max(time.Duration(0), stop-q.last)
	q.Above25msNS += above
	q.streak += above
	q.LongestAbove25msNS = max(q.LongestAbove25msNS, q.streak)
	if stop < now {
		q.streak = 0
	}
	q.FirstSecondAbove25msNS += max(time.Duration(0), min(stop, first+time.Second)-max(q.last, first))
	q.last = now
}

func (q *startupQueue) admit(now, serviceEnd, cost, first time.Duration, bytes int) bool {
	q.account(now, serviceEnd, first)
	q.OfferedPackets++
	q.OfferedBytes += int64(bytes)
	backlog := max(time.Duration(0), serviceEnd-now) + cost
	if backlog > time.Duration(q.Capacity*int64(time.Second)/q.Rate) {
		q.DroppedPackets++
		q.DroppedBytes += int64(bytes)
		if first > 0 && now <= first+time.Second {
			q.FirstSecondDrops++
		}
		return false
	}
	q.AdmittedPackets++
	q.AdmittedBytes += int64(bytes)
	q.PeakBacklogNS = max(q.PeakBacklogNS, backlog)
	if first > 0 && now <= first+time.Second {
		q.FirstSecondPeakNS = max(q.FirstSecondPeakNS, backlog)
	}
	return true
}

func TestVirtualTCPHyStartComparison(t *testing.T) {
	if got := runtime.GOMAXPROCS(0); got != 1 {
		t.Fatalf("GOMAXPROCS = %d, want 1", got)
	}
	for _, startup := range []string{"classic", "plus"} {
		for _, path := range []string{"control", "pulse", "shifted_pulse", "fifo"} {
			t.Run(startup+"_"+path, func(t *testing.T) {
				scenario := virtualScenario{
					delay: 50 * time.Millisecond, rate: 12500000, maxTime: 30 * time.Second,
					bytes: 16 << 20, dropOffset: -1, burstSegments: 1, streaming: true,
					startup: &startupPulse{LowerMSS: 30}, hyStartPlus: startup == "plus",
				}
				switch path {
				case "pulse":
					scenario.startup.DelayNS = 25 * time.Millisecond
				case "shifted_pulse":
					// The ACK covering a flight's end belongs to that flight
					// in RFC 9406. Cover the expected next round from ACK 29.
					scenario.startup.LowerMSS = 28
					scenario.startup.DelayNS = 25 * time.Millisecond
				case "fifo":
					// 10 Mbit/s and one nominal BDP of finite forward service.
					// The reverse path retains the open-link rate.
					scenario.forwardQueue = &startupQueue{Rate: 1250000, Capacity: 125000}
				}
				runVirtualTCP(t, "cubic", nil, scenario)
			})
		}
	}
}

func TestVirtualTCPHyStartTransientDelay(t *testing.T) {
	if got := runtime.GOMAXPROCS(0); got != 1 {
		t.Fatalf("GOMAXPROCS = %d, want 1", got)
	}
	for _, tc := range []struct {
		name  string
		delay time.Duration
	}{{"control", 0}, {"pulse", 25 * time.Millisecond}} {
		t.Run(tc.name, func(t *testing.T) {
			runVirtualTCP(t, "cubic", nil, virtualScenario{
				delay: 50 * time.Millisecond, rate: 12500000,
				maxTime: 30 * time.Second, bytes: 16 << 20,
				dropOffset: -1, burstSegments: 1, streaming: true, startup: &startupPulse{DelayNS: tc.delay, LowerMSS: 30},
			})
		})
	}
}

type senderObservation struct {
	AtNS          int64
	Cwnd          int
	Outstanding   int
	SackedOut     int
	SndUna        uint32
	SndNxt        uint32
	SndWnd        uint32
	BufferedBytes int
}

type streamRound struct {
	AtNS                          int64
	OfferedBytes                  int
	AcceptedBytes                 int
	DeliveredBytes                int
	AppRemainingBytes             int
	CapturedSequenceBytes         uint32
	CumulativelyAcknowledgedBytes uint32
	Cwnd                          uint32
	MSS                           uint32
	Ssthresh                      uint32
	RTTNS                         int64
	RTONS                         int64
	CCState                       tcpip.CongestionControlState
	RTOs                          uint64
	Retransmits                   uint64
	SACKRecoveries                uint64
	TLPRecoveries                 uint64
	// Probe is the latest post-receive-segment snapshot, not a claim that
	// timer or application-write processing invokes the existing probe.
	Probe senderObservation
}

// TestVirtualTCPBurstRecovery keeps the scenario fixed across algorithms and
// controls. It measures one finite recovery interval, not the private outage.
func TestVirtualTCPBurstRecovery(t *testing.T) {
	if got, want := runtime.GOMAXPROCS(0), 1; got != want {
		t.Fatalf("GOMAXPROCS = %d, want %d", got, want)
	}
	for _, algorithm := range []string{"reno", "cubic"} {
		t.Run(algorithm, func(t *testing.T) {
			for _, loss := range []struct {
				name   string
				offset int
			}{
				{"no_loss", -1}, {"burst_loss", 16 << 20},
			} {
				t.Run(loss.name, func(t *testing.T) {
					runVirtualTCP(t, algorithm, nil, virtualScenario{
						delay: 50 * time.Millisecond, rate: 12500000,
						maxTime: 30 * time.Second, bytes: 64 << 20,
						dropOffset: loss.offset, burstSegments: 8, streaming: true,
					})
				})
			}
		})
	}
}

func runVirtualTCP(t *testing.T, algorithm string, protocol stack.TransportProtocolFactory, scenario virtualScenario) {
	t.Helper()
	const quantum = time.Millisecond
	delay, rate, maxTime, dropOffset := scenario.delay, scenario.rate, scenario.maxTime, scenario.dropOffset
	clock := &completedClock{ManualClock: faketime.NewManualClock()}
	var stacks [2]*stack.Stack
	var links [2]*channel.Endpoint
	var pending []delivery
	var nextTX [2]time.Duration
	var trace []packetObservation
	var progress []recoveryObservation
	var received bytes.Buffer
	var client tcpip.Endpoint
	var probeMu sync.Mutex
	var latestProbe senderObservation
	var latestACK startupACK
	startup := scenario.startup
	flight := scenario.flight
	originalFIFO := flight != nil && flight.result.Role == originalRole
	queue := scenario.forwardQueue
	forwardRate := rate
	if queue != nil {
		forwardRate = queue.Rate
		if flight != nil {
			flight.result.ForwardRate, flight.result.QueueCapacity = queue.Rate, queue.Capacity
		}
	}
	var previousStartup *startupACK
	var startupEnded bool
	var lastReverseArrival time.Duration
	if scenario.streaming {
		newProbe := tcp.NewProtocolProbe
		if scenario.hyStartPlus {
			newProbe = tcp.NewProtocolProbeHyStartPlus
		}
		if flight != nil {
			newProbe = func(p tcp.TCPProbeFunc) func(*stack.Stack) stack.TransportProtocol {
				if originalFIFO {
					return tcp.NewProtocolProbeHyStartPlusWatch(p, flight.decision, flight.watch)
				}
				return tcp.NewProtocolProbeHyStartPlusRecovery(p, flight.decision)
			}
		}
		protocol = newProbe(func(state *tcp.TCPEndpointState) {
			if flight != nil {
				flight.endpoint(state)
			}
			if state.ID.LocalPort != 40000 || state.ID.RemotePort != 8080 {
				return
			}
			observation := senderObservation{
				AtNS: int64(state.SegTime.Sub(tcpip.MonotonicTime{})),
				Cwnd: state.Sender.SndCwnd, Outstanding: state.Sender.Outstanding,
				SackedOut: state.Sender.SackedOut,
				SndUna:    uint32(state.Sender.SndUna), SndNxt: uint32(state.Sender.SndNxt),
				SndWnd: uint32(state.Sender.SndWnd), BufferedBytes: state.SndBufState.SndBufUsed,
			}
			probeMu.Lock()
			latestProbe = observation
			if startup != nil {
				c := state.Sender.Cubic
				latestACK = startupACK{
					AtNS: observation.AtNS, RoundStartNS: int64(c.RoundStart.Sub(tcpip.MonotonicTime{})),
					LastAckNS: int64(c.LastAck.Sub(tcpip.MonotonicTime{})), SndUna: observation.SndUna, EndSeq: uint32(c.EndSeq), TSNow: state.TSOffset.TSVal(state.SegTime),
					Cwnd: observation.Cwnd, Ssthresh: state.Sender.Ssthresh, LastRTT: c.LastRTT, CurrRTT: c.CurrRTT, SampleCount: c.SampleCount,
					SndNxt: observation.SndNxt, Plus: c.HyStartPlus, CSS: c.CSS, CSSRounds: c.CSSRounds,
					CSSBaseline: c.CSSBaseline, ActualRTT: c.ACKRTT, Credit: c.StartupCredit,
					EpochNS: int64(c.T.Sub(tcpip.MonotonicTime{})), WMax: c.WMax, WEst: c.WEst,
				}
			}
			probeMu.Unlock()
		})
	}
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
		wantRecovery := tcpip.TCPRACKLossDetection
		if flight != nil && side == 0 {
			wantRecovery = flight.result.SenderMode
			if err := s.SetTransportProtocolOption(tcp.ProtocolNumber, &wantRecovery); err != nil {
				t.Fatal(err)
			}
		}
		if scenario.streaming {
			cc := tcpip.CongestionControlOption(algorithm)
			if err := s.SetTransportProtocolOption(tcp.ProtocolNumber, &cc); err != nil {
				t.Fatalf("set %s: %s", cc, err)
			}
			snd := tcpip.TCPSendBufferSizeRangeOption{Min: 4096, Default: 8 << 20, Max: 8 << 20}
			rcv := tcpip.TCPReceiveBufferSizeRangeOption{Min: 4096, Default: 8 << 20, Max: 8 << 20}
			if err := s.SetTransportProtocolOption(tcp.ProtocolNumber, &snd); err != nil {
				t.Fatal(err)
			}
			if err := s.SetTransportProtocolOption(tcp.ProtocolNumber, &rcv); err != nil {
				t.Fatal(err)
			}
		}
		var sack tcpip.TCPSACKEnabled
		if err := s.TransportProtocolOption(tcp.ProtocolNumber, &sack); err != nil || !sack {
			t.Fatalf("side %d SACK = %t, error = %v", side, sack, err)
		}
		var recovery tcpip.TCPRecovery
		if err := s.TransportProtocolOption(tcp.ProtocolNumber, &recovery); err != nil || recovery != wantRecovery {
			t.Fatalf("side %d recovery = %d, error = %v", side, recovery, err)
		}
		capacity := 1024
		if scenario.streaming {
			capacity = 16384
		}
		links[side] = channel.New(capacity, 1500, "")
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
	var acceptedBytes, packetCount, retainedSACKs int
	var burstDropped int
	var holes []packetObservation
	repaired := make(map[uint32]bool)
	var rounds []streamRound
	var preBurst *streamRound
	var demandEndedNS, holesAcknowledgedNS int64
	var demandEndedDeliveredBytes, holesAcknowledgedDeliveredBytes int
	var maxOutstandingBeforeBurst uint32
	allPackets := sha256.New()
	sample := func() streamRound {
		probeMu.Lock()
		probe := latestProbe
		probeMu.Unlock()
		var info tcpip.TCPInfoOption
		if err := client.GetSockOpt(&info); err != nil {
			t.Fatalf("TCPInfo: %s", err)
		}
		stats := stacks[0].Stats().TCP
		return streamRound{
			AtNS: int64(now()), OfferedBytes: scenario.bytes, AcceptedBytes: acceptedBytes,
			DeliveredBytes: received.Len(), AppRemainingBytes: scenario.bytes - acceptedBytes,
			CapturedSequenceBytes: highestSent, CumulativelyAcknowledgedBytes: acknowledged,
			Cwnd: info.SndCwnd, MSS: info.SndMss, Ssthresh: info.SndSsthresh,
			RTTNS: int64(info.RTT), RTONS: int64(info.RTO), CCState: info.CcState,
			RTOs: stats.Timeouts.Value(), Retransmits: stats.Retransmits.Value(),
			SACKRecoveries: stats.SACKRecovery.Value(), TLPRecoveries: stats.TLPRecovery.Value(), Probe: probe,
		}
	}

	defer func() {
		encodedTrace, err := json.Marshal(trace)
		if err != nil {
			t.Fatal(err)
		}
		result := struct {
			Case                            string
			Algorithm                       string
			DropOffset                      int
			LostSequence                    uint32
			LostEnd                         uint32
			RecoveryTrace                   []recoveryObservation
			VirtualNS                       int64
			Bytes                           int
			PayloadSHA256                   string
			Retransmits                     uint64
			RTOs                            uint64
			SACKRecoveries                  uint64
			TLPRecoveries                   uint64
			RecoveryClass                   string
			TraceSHA256                     string
			Trace                           []packetObservation
			OfferedBytes                    int
			AcceptedBytes                   int
			BDPBytes                        int64
			RateBytesPerSecond              int64
			OneWayDelayNS                   int64
			AllPacketsSHA256                string
			PacketCount                     int
			PreBurst                        *streamRound
			Rounds                          []streamRound
			Holes                           []packetObservation
			HolesAcknowledgedNS             int64
			DemandEndedNS                   int64
			DemandEndedDeliveredBytes       int
			HolesAcknowledgedDeliveredBytes int
			MaxOutstandingBeforeBurst       uint32
			Startup                         *startupPulse `json:",omitempty"`
			ForwardQueue                    *startupQueue `json:",omitempty"`
			Flight                          *flightResult `json:",omitempty"`
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
			TraceSHA256:  fmt.Sprintf("%x", sha256.Sum256(encodedTrace)),
			Trace:        trace,
			OfferedBytes: scenario.bytes, AcceptedBytes: acceptedBytes,
			BDPBytes: forwardRate * int64(2*delay) / int64(time.Second), RateBytesPerSecond: forwardRate, OneWayDelayNS: int64(delay),
			AllPacketsSHA256: fmt.Sprintf("%x", allPackets.Sum(nil)), PacketCount: packetCount,
			PreBurst: preBurst, Rounds: rounds, Holes: holes,
			HolesAcknowledgedNS: holesAcknowledgedNS, DemandEndedNS: demandEndedNS,
			DemandEndedDeliveredBytes: demandEndedDeliveredBytes, HolesAcknowledgedDeliveredBytes: holesAcknowledgedDeliveredBytes,
			MaxOutstandingBeforeBurst: maxOutstandingBeforeBurst,
			Startup:                   startup,
			ForwardQueue:              queue,
		}
		if flight != nil {
			result.Flight = &flight.result
			// Replay reports the frozen exposure and any protected ingress
			// waiting. Only the reference uses the original drop-tail counters.
			if flight.ref != nil {
				result.ForwardQueue = nil
			}
		}
		encoded, err := json.Marshal(result)
		if err != nil {
			t.Fatal(err)
		}
		if flight != nil {
			// Full bidirectional traces can be many MB. Keep individual
			// native test log lines bounded while retaining all raw events.
			const limit = 8192
			count := (len(encoded) + limit - 1) / limit
			for n := 0; n < count; n++ {
				t.Logf("VIRTUAL_TCP_PART %s %d/%d %s", t.Name(), n, count, encoded[n*limit:min(len(encoded), (n+1)*limit)])
			}
			return
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
					if scenario.streaming {
						previous.TimerCallbacks = observation.TimerCallbacks
					}
					changed = previous != observation
				}
				if changed {
					if scenario.streaming && len(progress) == 512 {
						t.Fatal("recovery events exceeded512 records")
					}
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
						if flight != nil {
							flight.header(side, segment.Flags(), segment.SequenceNumber(), len(payload), int64(now()))
						}
						payloadHash := fmt.Sprintf("%x", sha256.Sum256(payload))
						var retransmitted, isLoss bool
						if side == 0 && len(payload) != 0 {
							if !dataStarted {
								dataSequence, dataStarted = segment.SequenceNumber(), true
								if startup != nil {
									startup.DataSequence = dataSequence
									startup.FirstPayloadNS = int64(now())
								}
							}
							offset := segment.SequenceNumber() - dataSequence
							retransmitted = offset < highestSent
							highestSent = max(highestSent, offset+uint32(len(payload)))
							if !dropped {
								maxOutstandingBeforeBurst = max(maxOutstandingBeforeBurst, highestSent-acknowledged)
							}
							beginsBurst := dropOffset >= 0 && !dropped && int(offset) <= dropOffset && dropOffset < int(offset)+len(payload)
							isLoss = !retransmitted && (beginsBurst || (dropped && burstDropped < scenario.burstSegments))
							if isLoss {
								if !dropped {
									dropped, lostSequence, lostPayload = true, segment.SequenceNumber(), payloadHash
									if scenario.streaming {
										row := sample()
										preBurst = &row
									}
								} else if segment.SequenceNumber() != lostEnd {
									sequence := segment.SequenceNumber()
									view.Release()
									packet.DecRef()
									t.Fatalf("burst segment starts %d, want %d", sequence, lostEnd)
								}
								burstDropped++
								lostEnd = segment.SequenceNumber() + uint32(len(payload))
							} else if dropped && segment.SequenceNumber() == lostSequence && segment.SequenceNumber()+uint32(len(payload)) == lostEnd && payloadHash == lostPayload {
								recovered = true
							}
						}
						var sackPermitted bool
						if segment.Flags().Contains(header.TCPFlagSyn) {
							sackPermitted = header.ParseSynOptions(segment.Options(), segment.Flags().Contains(header.TCPFlagAck)).SACKPermitted
						}

						linkRate := rate
						if side == 0 {
							linkRate = forwardRate
						}
						cost := time.Duration((int64(packet.Size())*int64(time.Second) + linkRate - 1) / linkRate)
						at := now()
						var refused bool
						if side == 0 && queue != nil {
							if flight != nil && flight.ref != nil && len(payload) > 0 {
								refused = flight.replayDrop(segment.SequenceNumber()-dataSequence, len(payload), retransmitted, now(), nextTX[side], cost, time.Duration(queue.Capacity*int64(time.Second)/queue.Rate))
							} else {
								refused = !queue.admit(now(), nextTX[side], cost, time.Duration(startup.FirstPayloadNS), packet.Size())
							}
						}
						if refused {
							// Overflow is rejected at enqueue and consumes no service.
							isLoss = true
						} else {
							// The separate scripted loss injector drops after serialization.
							nextTX[side] = max(nextTX[side], now()) + cost
							at = nextTX[side] + delay
						}
						nominal := at
						if startup != nil && side == 1 {
							offset := segment.AckNumber() - dataSequence
							if dataStarted && segment.Flags() == header.TCPFlagAck && len(payload) == 0 && offset > startup.LowerMSS*startup.MSS && offset <= (startup.LowerMSS+40)*startup.MSS {
								at += startup.DelayNS
							}
							// A following ACK cannot pass an earlier one on this reverse path.
							at = max(at, lastReverseArrival)
							lastReverseArrival = at
						}
						row := packetObservation{
							AtNS: int64(at), CapturedAtNS: int64(now()), From: side,
							Sequence: segment.SequenceNumber(), EndSequence: segment.SequenceNumber() + uint32(len(payload)),
							Acknowledgment: segment.AckNumber(), Flags: uint8(segment.Flags()), PayloadBytes: len(payload),
							PacketSHA256: fmt.Sprintf("%x", sha256.Sum256(view.AsSlice())), PayloadSHA256: payloadHash,
							OptionsHex: fmt.Sprintf("%x", segment.Options()), SACKBlocks: header.ParseTCPOptions(segment.Options()).SACKBlocks,
							SACKPermitted: sackPermitted, Retransmitted: retransmitted, Dropped: isLoss,
						}
						if startup != nil {
							row.NominalAtNS = int64(nominal)
							if side == 1 && segment.Flags() == header.TCPFlagAck {
								row.TSEcr = header.ParseTCPOptions(segment.Options()).TSEcr
							}
						}
						if flight != nil {
							opt := header.ParseTCPOptions(segment.Options())
							row.WireWindow, row.WireTSVal, row.TSEcr = segment.WindowSize(), opt.TSVal, opt.TSEcr
							flight.offer(row)
						}
						if isLoss && dropOffset >= 0 {
							holes = append(holes, row)
						}
						if retransmitted {
							for _, hole := range holes {
								if row.Sequence == hole.Sequence && row.EndSequence == hole.EndSequence && row.PayloadSHA256 == hole.PayloadSHA256 {
									repaired[hole.Sequence] = true
								}
							}
						}
						pending = append(pending, delivery{at: at, from: side, packet: stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: packet.ToBuffer()}), row: row})
						if len(pending) > 32768 {
							view.Release()
							packet.DecRef()
							t.Fatal("virtual link exceeded 32768 queued packets")
						}
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
	originalDone := false
	step := func() {
		t.Helper()
		fence()
		if now() >= maxTime {
			if originalFIFO {
				// This is the original failure boundary: the application has
				// performed its final Write, but no more clock time is allowed.
				flight.mu.Lock()
				if flight.result.End != "" {
					previous := flight.result.End
					flight.mu.Unlock()
					t.Fatalf("original observer ended before deadline: %s", previous)
				}
				originalDone = true
				flight.result.Match = "original_deadline_incomplete"
				flight.result.End = "original_deadline_incomplete"
				flight.result.EndNS, flight.result.EndAck = int64(now())-flight.first, acknowledged
				flight.mu.Unlock()
				return
			}
			t.Fatalf("transfer exceeded %v virtual time; queued=%d", maxTime, len(pending))
		}
		next := now() + quantum
		if flight != nil && !originalFIFO {
			next = min(next, maxTime)
		}
		if startup != nil && dataStarted && startup.Deadline == nil {
			next = min(next, time.Duration(startup.FirstPayloadNS)+time.Second)
		}
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
		var startupACKPacket *packetObservation
		var dataACK *packetObservation
		var trackStartup bool
		if index != -1 {
			packet := pending[index]
			pending = append(pending[:index], pending[index+1:]...)
			if flight != nil {
				flight.ingress(packet.row)
			}
			packetCount++
			hashRow := packet.row
			if originalFIFO {
				// Hash exactly the pre-observer row, using the original
				// timestamp readout (echo only on a reverse ACK).
				hashRow.WireWindow, hashRow.WireTSVal = 0, 0
				if hashRow.From != 1 || hashRow.Flags != uint8(header.TCPFlagAck) {
					hashRow.TSEcr = 0
				}
			}
			encoded, err := json.Marshal(hashRow)
			if err != nil {
				packet.packet.DecRef()
				t.Fatal(err)
			}
			allPackets.Write(encoded)
			allPackets.Write([]byte{'\n'})
			retain := !scenario.streaming || (packet.row.Dropped || packet.row.Retransmitted) && (queue == nil || len(trace) < 400) || header.TCPFlags(packet.row.Flags).Contains(header.TCPFlagSyn) || header.TCPFlags(packet.row.Flags).Contains(header.TCPFlagFin)
			if startup != nil && dataStarted {
				if packet.from == 0 && packet.row.PayloadBytes != 0 && packet.row.Sequence-dataSequence < startupTraceMSS*startup.MSS {
					retain = true
				}
				offset := packet.row.Acknowledgment - dataSequence
				if packet.from == 1 && packet.row.Flags == uint8(header.TCPFlagAck) && offset > 0 && offset <= highestSent {
					dataACK = &packet.row
					trackStartup = !startupEnded
				}
				if packet.from == 1 && packet.row.Flags == uint8(header.TCPFlagAck) && offset > 0 && offset <= startupTraceMSS*startup.MSS {
					startupACKPacket = &packet.row
					retain = true
				}
			}
			if scenario.streaming && len(packet.row.SACKBlocks) != 0 && retainedSACKs < 32 {
				retainedSACKs++
				retain = true
			}
			if retain {
				if scenario.streaming && len(trace) == 512 {
					packet.packet.DecRef()
					t.Fatal("compact trace exceeded512 exact packet records")
				}
				trace = append(trace, packet.row)
			}
			if !packet.row.Dropped {
				links[1-packet.from].InjectInbound(ipv4.ProtocolNumber, packet.packet)
				if packet.from == 1 && dataStarted && header.TCPFlags(packet.row.Flags).Contains(header.TCPFlagAck) {
					acknowledged = max(acknowledged, packet.row.Acknowledgment-dataSequence)
					if scenario.streaming {
						// This observation counts data bytes, excluding FIN's
						// extra sequence number at transfer completion.
						acknowledged = min(acknowledged, highestSent)
					}
					if scenario.streaming && burstDropped == scenario.burstSegments && holesAcknowledgedNS == 0 && acknowledged >= lostEnd-dataSequence {
						holesAcknowledgedNS = int64(now())
						holesAcknowledgedDeliveredBytes = received.Len()
					}
				}
			}
			packet.packet.DecRef()
		}
		fence()
		if flight != nil {
			flight.endInput()
		}
		if dataACK != nil || startupACKPacket != nil {
			probeMu.Lock()
			ack := latestACK
			probeMu.Unlock()
			if ack.AtNS != int64(now()) {
				t.Fatalf("sender probe at %d, want ACK ingress %d", ack.AtNS, now())
			}
			startup.PeakCwnd = max(startup.PeakCwnd, ack.Cwnd)
			if int64(now()) <= startup.FirstPayloadNS+int64(time.Second) {
				startup.FirstSecondPeakCwnd = max(startup.FirstSecondPeakCwnd, ack.Cwnd)
			}
			if startupACKPacket != nil {
				if ack.SndUna != startupACKPacket.Acknowledgment {
					t.Fatalf("sender ACK %d, want wire ACK %d", ack.SndUna, startupACKPacket.Acknowledgment)
				}
				startup.ACKs = append(startup.ACKs, ack)
			}
			if trackStartup {
				before := previousStartup
				if before == nil {
					before = &ack
				}
				if previousStartup == nil || before.RoundStartNS != ack.RoundStartNS || before.CSS != ack.CSS || before.CSSRounds != ack.CSSRounds || before.Ssthresh != ack.Ssthresh || before.EndSeq != ack.EndSeq {
					if len(startup.Transitions) == 256 {
						t.Fatal("startup exceeded 256 round or phase transitions")
					}
					if before.SndUna != ack.SndUna && dataACK.Acknowledgment != ack.SndUna {
						t.Fatalf("advancing transition probe ACK %d, want wire ACK %d", ack.SndUna, dataACK.Acknowledgment)
					}
					startup.Transitions = append(startup.Transitions, startupTransition{Before: *before, After: ack,
						WireAtNS: dataACK.AtNS, WireACK: dataACK.Acknowledgment, WireTSEcr: dataACK.TSEcr})
				}
				startupEnded = ack.Ssthresh != tcp.InitialSsthresh
				previousStartup = &ack
			}
		}
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
	listener := newEndpoint(1)
	client = newEndpoint(0)
	if scenario.streaming {
		client.SocketOptions().SetSendBufferSize(8<<20, true)
		listener.SocketOptions().SetReceiveBufferSize(8<<20, true)
		if got, want := client.SocketOptions().GetSendBufferSize(), int64(8<<20); got != want {
			t.Fatalf("send buffer=%d, want%d", got, want)
		}
		if got, want := listener.SocketOptions().GetReceiveBufferSize(), int64(8<<20); got != want {
			t.Fatalf("receive buffer=%d, want%d", got, want)
		}
	}
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
	if startup != nil {
		startup.MSS = sample().MSS
		if startup.MSS == 0 {
			t.Fatal("established sender has zero MSS")
		}
	}
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
	data := make([]byte, scenario.bytes)
	for i := range data {
		data[i] = byte(i*17 + i/251)
	}
	if !scenario.streaming {
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
		acceptedBytes = len(data)
	}
	var writeClosed bool
	nextRound := now()
	if !scenario.streaming {
		writeClosed = true
	}
	var eof bool
	for {
		if scenario.streaming && acceptedBytes < len(data) {
			n, err := client.Write(bytes.NewReader(data[acceptedBytes:]), tcpip.WriteOptions{})
			if _, blocked := err.(*tcpip.ErrWouldBlock); err != nil && !blocked {
				t.Fatalf("stream Write: %s", err)
			}
			acceptedBytes += int(n)
			if acceptedBytes == len(data) {
				demandEndedNS = int64(now())
				demandEndedDeliveredBytes = received.Len()
			}
		}
		if scenario.streaming && acceptedBytes == len(data) && !writeClosed {
			if err := client.Shutdown(tcpip.ShutdownWrite); err != nil {
				t.Fatal(err)
			}
			writeClosed = true
		}
		step()
		if originalDone {
			// The archived transfer was incomplete at this exact virtual
			// instant with the same full wire digest and delivered bytes.
			if now() != 30000331840*time.Nanosecond || received.Len() != 1121800 || acceptedBytes != 9510408 ||
				!bytes.Equal(received.Bytes(), data[:received.Len()]) || fmt.Sprintf("%x", allPackets.Sum(nil)) != "98c0724b98db9f9590c94924f489f1d35e4dddd72895caea4aa06bf22211827b" {
				t.Fatalf("original deadline/delivered payload/packet digest changed: t=%v delivered=%d accepted=%d digest=%x", now(), received.Len(), acceptedBytes, allPackets.Sum(nil))
			}
			classification = flight.result.End
			return
		}
		_, err := server.Read(&received, tcpip.ReadOptions{})
		switch err.(type) {
		case nil, *tcpip.ErrWouldBlock:
		case *tcpip.ErrClosedForReceive:
			eof = true
		default:
			t.Fatalf("Read: %s", err)
		}
		fence()
		if flight != nil && !originalFIFO && flight.stop(int64(now()), acknowledged, int64(maxTime)) {
			if received.Len() > len(data) || !bytes.Equal(received.Bytes(), data[:received.Len()]) {
				t.Fatalf("received bytes do not match the offered prefix")
			}
			checkAlgorithm()
			rounds = append(rounds, sample())
			classification = flight.result.End
			return
		}
		if startup != nil && dataStarted && startup.Deadline == nil && now() == time.Duration(startup.FirstPayloadNS)+time.Second {
			row := sample()
			startup.Deadline = &row
		}
		if scenario.streaming && now() >= nextRound {
			rounds = append(rounds, sample())
			nextRound += 100 * time.Millisecond
		}
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
	if scenario.streaming {
		rounds = append(rounds, sample())
		if dropOffset >= 0 {
			if got, want := len(holes), scenario.burstSegments; got != want {
				t.Fatalf("holes=%d,want%d", got, want)
			}
			if got, want := len(repaired), len(holes); got != want {
				t.Fatalf("repaired exact ranges=%d,want%d", got, want)
			}
			if holesAcknowledgedNS == 0 {
				t.Fatal("burst was not cumulatively acknowledged")
			}
			recovered = true
		}
	}
	retransmits := stacks[0].Stats().TCP.Retransmits.Value()
	if dropOffset >= 0 {
		if !dropped || !recovered || retransmits == 0 {
			t.Fatalf("loss recovery: dropped=%t, retransmitted exact payload=%t, retransmits=%d", dropped, recovered, retransmits)
		}
	} else if queue == nil && retransmits != 0 {
		t.Fatalf("no-loss retransmits = %d, want 0", retransmits)
	}
	stats := stacks[0].Stats().TCP
	if queue != nil {
		queue.account(now(), nextTX[0], time.Duration(startup.FirstPayloadNS))
		classification = "finite_fifo"
	} else if dropOffset < 0 {
		classification = "no_loss"
	} else if scenario.streaming {
		classification = "burst_recovered"
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
