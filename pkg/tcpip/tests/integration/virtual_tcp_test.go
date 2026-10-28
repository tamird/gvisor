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
	flow   int
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
	measureWindow bool
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

// recoveryWindow retains exact common-time samples and the packet records
// needed to distinguish application delivery, wire service, and ACK policy.
type recoveryWindow struct {
	Start streamRound
	End   streamRound
	// Direction, capture, service start/end, arrival, wire/payload bytes,
	// retransmission, drop, TCP flags, and ACK number.
	Packets [][11]int64
}

const (
	recoveryWindowStart = 3 * time.Second
	recoveryWindowEnd   = 5 * time.Second
)

// TestVirtualTCPRecoveryWindow compares clean and burst-loss recovery on the
// same path using fixed 3 s and 5 s observations, while each independent sender has demand.
func TestVirtualTCPRecoveryWindow(t *testing.T) {
	testVirtualTCPBurstRecovery(t, true)
}

// TestVirtualTCPBurstRecovery keeps the scenario fixed across algorithms and
// controls. It measures one finite recovery interval, not the private outage.
func TestVirtualTCPBurstRecovery(t *testing.T) {
	testVirtualTCPBurstRecovery(t, false)
}

func testVirtualTCPBurstRecovery(t *testing.T, measureWindow bool) {
	t.Helper()
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
						measureWindow: measureWindow,
					})
				})
			}
		})
	}
}

// drainVirtualTCP is a workload-specific fence: endpoint calls have returned,
// link injection is stopped, and each stack has one processor. It is not a
// general quiescence interface for arbitrary background work.
func drainVirtualTCP(t *testing.T, clock *completedClock, stacks []*stack.Stack, observe func()) {
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
			observe()
			return true
		}()
		if stable {
			return
		}
	}
	t.Fatal("TCP callbacks did not reach the bounded same-instant fence")
}

func advanceVirtualTCP(clock *completedClock, stacks []*stack.Stack, duration time.Duration) {
	for _, s := range stacks {
		s.Pause()
	}
	defer func() {
		for _, s := range stacks {
			s.Resume()
		}
	}()
	// Keep processors parked while callbacks observe intermediate deadlines.
	clock.Advance(duration)
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
	if scenario.streaming {
		protocol = tcp.NewProtocolProbe(func(state *tcp.TCPEndpointState) {
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
		if err := s.TransportProtocolOption(tcp.ProtocolNumber, &recovery); err != nil || recovery != tcpip.TCPRACKLossDetection {
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
	var window *recoveryWindow
	if scenario.measureWindow {
		window = &recoveryWindow{}
	}
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
			Window                          *recoveryWindow `json:",omitempty"`
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
			BDPBytes: rate * int64(2*delay) / int64(time.Second), RateBytesPerSecond: rate, OneWayDelayNS: int64(delay),
			AllPacketsSHA256: fmt.Sprintf("%x", allPackets.Sum(nil)), PacketCount: packetCount,
			PreBurst: preBurst, Rounds: rounds, Holes: holes,
			HolesAcknowledgedNS: holesAcknowledgedNS, DemandEndedNS: demandEndedNS,
			DemandEndedDeliveredBytes: demandEndedDeliveredBytes, HolesAcknowledgedDeliveredBytes: holesAcknowledgedDeliveredBytes,
			MaxOutstandingBeforeBurst: maxOutstandingBeforeBurst, Window: window,
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
		drainVirtualTCP(t, clock, stacks[:], func() {
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
					payloadHash := fmt.Sprintf("%x", sha256.Sum256(payload))
					var retransmitted, isLoss bool
					if side == 0 && len(payload) != 0 {
						if !dataStarted {
							dataSequence, dataStarted = segment.SequenceNumber(), true
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

					// Dropped packets still consume their serialization time.
					serviceStart := max(nextTX[side], now())
					nextTX[side] = serviceStart + time.Duration((int64(packet.Size())*int64(time.Second)+rate-1)/rate)
					at := nextTX[side] + delay
					if window != nil && ((now() >= recoveryWindowStart && now() <= recoveryWindowEnd) ||
						(serviceStart < recoveryWindowEnd && nextTX[side] > recoveryWindowStart) ||
						(at >= recoveryWindowStart && at <= recoveryWindowEnd)) {
						if got, limit := len(window.Packets), 60000; got >= limit {
							view.Release()
							packet.DecRef()
							t.Fatalf("window packet count = %d, limit %d", got, limit)
						}
						var retransmission, drop int64
						if retransmitted {
							retransmission = 1
						}
						if isLoss {
							drop = 1
						}
						window.Packets = append(window.Packets, [11]int64{int64(side), int64(now()),
							int64(serviceStart), int64(nextTX[side]), int64(at), int64(packet.Size()),
							int64(len(payload)), retransmission, drop, int64(segment.Flags()), int64(segment.AckNumber())})
					}
					row := packetObservation{
						AtNS: int64(at), CapturedAtNS: int64(now()), From: side,
						Sequence: segment.SequenceNumber(), EndSequence: segment.SequenceNumber() + uint32(len(payload)),
						Acknowledgment: segment.AckNumber(), Flags: uint8(segment.Flags()), PayloadBytes: len(payload),
						PacketSHA256: fmt.Sprintf("%x", sha256.Sum256(view.AsSlice())), PayloadSHA256: payloadHash,
						OptionsHex: fmt.Sprintf("%x", segment.Options()), SACKBlocks: header.ParseTCPOptions(segment.Options()).SACKBlocks,
						SACKPermitted: sackPermitted, Retransmitted: retransmitted, Dropped: isLoss,
					}
					if isLoss {
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
		})
	}
	step := func() {
		t.Helper()
		fence()
		if now() >= maxTime {
			t.Fatalf("transfer exceeded %v virtual time; queued=%d", maxTime, len(pending))
		}
		next := now() + quantum
		if window != nil {
			for _, boundary := range [...]time.Duration{recoveryWindowStart, recoveryWindowEnd} {
				if now() < boundary && boundary < next {
					next = boundary
				}
			}
		}
		index := -1
		for i, packet := range pending {
			if packet.at < next || (packet.at == next && index == -1) {
				next, index = packet.at, i
			}
		}
		advanceVirtualTCP(clock, stacks[:], next-now())
		// Timer-generated output is observed at this tick's end. The link
		// schedule models that explicit 1 ms resolution, not exact emission.
		fence()
		if index != -1 {
			packet := pending[index]
			pending = append(pending[:index], pending[index+1:]...)
			packetCount++
			encoded, err := json.Marshal(packet.row)
			if err != nil {
				packet.packet.DecRef()
				t.Fatal(err)
			}
			allPackets.Write(encoded)
			allPackets.Write([]byte{'\n'})
			retain := !scenario.streaming || packet.row.Dropped || packet.row.Retransmitted || header.TCPFlags(packet.row.Flags).Contains(header.TCPFlagSyn) || header.TCPFlags(packet.row.Flags).Contains(header.TCPFlagFin)
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
		_, err := server.Read(&received, tcpip.ReadOptions{})
		switch err.(type) {
		case nil, *tcpip.ErrWouldBlock:
		case *tcpip.ErrClosedForReceive:
			eof = true
		default:
			t.Fatalf("Read: %s", err)
		}
		fence()
		if scenario.streaming && now() >= nextRound {
			rounds = append(rounds, sample())
			nextRound += 100 * time.Millisecond
		}
		if window != nil && (now() == recoveryWindowStart || now() == recoveryWindowEnd) {
			// Drain all equal-time deliveries before taking the boundary snapshot.
			pendingNow := false
			for _, packet := range pending {
				pendingNow = pendingNow || packet.at <= now()
			}
			if !pendingNow {
				row := sample()
				if row.AppRemainingBytes <= 0 || row.AcceptedBytes <= int(row.CumulativelyAcknowledgedBytes) {
					t.Fatalf("sender is not backlogged at %v: %+v", now(), row)
				}
				if now() == recoveryWindowStart {
					window.Start = row
				} else {
					window.End = row
				}
			}
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
	if window != nil {
		if got, want := window.Start.AtNS, int64(recoveryWindowStart); got != want {
			t.Fatalf("window start = %d, want %d", got, want)
		}
		if got, want := window.End.AtNS, int64(recoveryWindowEnd); got != want {
			t.Fatalf("window end = %d, want %d", got, want)
		}
	}
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
	} else if retransmits != 0 {
		t.Fatalf("no-loss retransmits = %d, want 0", retransmits)
	}
	stats := stacks[0].Stats().TCP
	if dropOffset < 0 {
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
