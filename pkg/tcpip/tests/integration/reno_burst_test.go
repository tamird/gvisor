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
	"strings"
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

const (
	dataBytes = 512 << 10
	horizon   = 35 * time.Second
	rate      = 2500000 // Per-direction link serialization in bytes per second.
	delay     = 10 * time.Millisecond
	tick      = time.Millisecond
)

type completedClock struct {
	*faketime.ManualClock
	callbacks atomicbitops.Uint64
}

func (c *completedClock) AfterFunc(d time.Duration, fn func()) tcpip.Timer {
	return c.ManualClock.AfterFunc(d, func() {
		fn()
		c.callbacks.Add(1)
	})
}

type zeroReader struct{}

func (zeroReader) Read(b []byte) (int, error) {
	clear(b)
	return len(b), nil
}

// forward loss coordinates count physical data offers, including retransmissions.
type lossSchedule struct {
	Name        string
	First, Last int
}

const frontierByte = 128 << 10

func (l lossSchedule) frontier() bool {
	return l.Name == "frontier_control" || l.Name == "frontier_loss"
}

type wire struct {
	At, Due                       int64
	From, Offer                   int
	Seq, ACK                      uint32
	Flags                         header.TCPFlags
	Bytes                         int
	Hash                          string
	TS, Permitted, Dropped, Retry bool
	SACK                          []header.SACKBlock
	QueueDrop, Injected           bool `json:",omitempty"`
	Occupancy                     int  `json:",omitempty"`
	IPBytes                       int  `json:",omitempty"`
}

type event struct {
	Wire    *wire            `json:",omitempty"`
	Sender  *tcp.RTOEvent    `json:",omitempty"`
	Ingress *frontierIngress `json:",omitempty"`
}

type frontierIngress struct {
	At     int64
	Packet wire
}

type trial struct {
	Case, Algorithm, KernelOS, KernelArch, Go        string
	Schedule                                         lossSchedule
	GuardACK, GuardTimer, EligibleACK, EligibleTimer bool
	FirstData, End                                   int64
	Offered, Accepted, Delivered, Pending            int
	Acknowledged                                     uint32
	BodyHash                                         string
	EOF, FinACK                                      bool
	Drops, Offers                                    int
	QueueDrops                                       [2]int  `json:",omitempty"`
	MaxOccupancy                                     [2]int  `json:",omitempty"`
	WirePackets                                      int     `json:",omitempty"`
	WireHash                                         string  `json:",omitempty"`
	EchoNS                                           []int64 `json:",omitempty"`
	Retransmissions, RTOs, SACKRecoveries            uint64
	Events                                           []event
}

func TestVirtualTCPNoTimestampBurst(t *testing.T) {
	if runtime.GOMAXPROCS(0) != 1 {
		t.Fatal("one TCP processor per stack is required")
	}
	for _, schedule := range []lossSchedule{{"none", 0, 0}, {"blackout", 1, 11}, {"partial", 2, 8}} {
		var base trial
		for _, cc := range []string{"reno", "cubic"} {
			t.Run(cc+"_"+schedule.Name, func(t *testing.T) {
				row := runTrial(t, cc, schedule, false, false)
				if cc == "reno" {
					base = row
				}
			})
		}
		if base.EligibleACK {
			t.Run("reno_"+schedule.Name+"_ack", func(t *testing.T) { runTrial(t, "reno", schedule, true, false) })
		}
		if base.EligibleTimer {
			t.Run("reno_"+schedule.Name+"_timer", func(t *testing.T) { runTrial(t, "reno", schedule, false, true) })
		}
		if base.EligibleACK && base.EligibleTimer {
			t.Run("reno_"+schedule.Name+"_both", func(t *testing.T) { runTrial(t, "reno", schedule, true, true) })
		}
	}
}

func runTrial(t *testing.T, algorithm string, loss lossSchedule, ackGuard, timerGuard bool) trial {
	t.Helper()
	frontier := loss.frontier()
	demand, buffer := dataBytes, int64(1<<20)
	rates := [2]int64{rate, rate}
	latency := delay
	if frontier {
		demand, buffer = 4<<20, 8<<20
		rates = [2]int64{1250000, 12500000}
		latency = 50 * time.Millisecond
	}
	row := trial{Case: t.Name(), Algorithm: algorithm, KernelOS: runtime.GOOS, KernelArch: runtime.GOARCH, Go: runtime.Version(),
		Schedule: loss, Offered: demand, GuardACK: ackGuard, GuardTimer: timerGuard}
	clock := &completedClock{ManualClock: faketime.NewManualClock()}
	now := func() time.Duration { return clock.NowMonotonic().Sub(tcpip.MonotonicTime{}) }
	// As in the existing virtual transfer driver, clock advancement and both
	// TCP processors are fenced. Nothing outside the test drives the link.
	var mu sync.Mutex
	var watching bool
	var recoveryPoint uint32
	var base uint32
	var haveBase, windowDone bool
	wireHash := sha256.New()
	record := func(w *wire) {
		mu.Lock()
		defer mu.Unlock()
		if frontier {
			b, err := json.Marshal(w)
			if err != nil {
				t.Fatal(err)
			}
			wireHash.Write(b)
			wireHash.Write([]byte{'\n'})
			row.WirePackets++
			if windowDone && !w.Dropped {
				return
			}
		}
		row.Events = append(row.Events, event{Wire: w})
	}
	probe := tcp.RTODiagnostic{GuardACK: ackGuard, GuardTimer: timerGuard, Timestamps: frontier, Observe: func(r tcp.RTOEvent) {
		mu.Lock()
		defer mu.Unlock()
		if frontier {
			if !windowDone || strings.HasSuffix(r.Kind, "_cc") || r.Kind == "rto_interrupt" || r.Kind == "enter_recovery" {
				row.Events = append(row.Events, event{Sender: &r})
			}
			if haveBase && r.Kind == "ack_end" && int32(r.UNA-base) >= frontierByte+(192<<10) {
				windowDone = true
			}
			return
		}
		if r.Kind == "rto_enter" {
			watching, recoveryPoint = true, r.Last
		}
		if r.State == tcpip.RTORecovery {
			row.EligibleACK = row.EligibleACK || (r.Kind == "ack_scan" && r.Lost > 0 && !r.Active)
			row.EligibleTimer = row.EligibleTimer || r.Kind == "timer_expired"
		}
		if watching || r.State != tcpip.Open || len(r.SACK) != 0 || r.Lost != 0 || strings.HasSuffix(r.Kind, "_cc") ||
			r.Kind == "rto_interrupt" || r.Kind == "enter_recovery" || r.Kind == "timer_expired" || r.Kind == "timer_scan" {
			row.Events = append(row.Events, event{Sender: &r})
		}
		if watching && r.Kind == "ack_end" && int32(r.UNA-recoveryPoint) > 0 {
			watching = false
		}
	}}
	var stacks [2]*stack.Stack
	var links [2]*channel.Endpoint
	type delivery struct {
		at     time.Duration
		from   int
		packet *stack.PacketBuffer
		row    wire
	}
	var pending []delivery
	var tx [2]time.Duration
	addr := [2]tcpip.Address{tcpip.AddrFrom4([4]byte{10, 0, 0, 1}), tcpip.AddrFrom4([4]byte{10, 0, 0, 2})}
	t.Cleanup(func() {
		for _, s := range stacks {
			if s != nil {
				s.Destroy()
			}
		}
		for _, l := range links {
			if l != nil {
				l.Close()
			}
		}
		for _, p := range pending {
			p.packet.DecRef()
		}
	})
	for side := range stacks {
		protocol := stack.TransportProtocolFactory(tcp.NewProtocol)
		if side == 0 {
			protocol = tcp.NewProtocolRTODiagnostic(algorithm, &probe)
		}
		s := stack.New(stack.Options{NetworkProtocols: []stack.NetworkProtocolFactory{ipv4.NewProtocol},
			TransportProtocols: []stack.TransportProtocolFactory{protocol}, Clock: clock, ClockResolution: tick, SecureRNG: zeroReader{}})
		stacks[side] = s
		var sack tcpip.TCPSACKEnabled
		var rack tcpip.TCPRecovery
		if err := s.TransportProtocolOption(tcp.ProtocolNumber, &sack); err != nil || !sack {
			t.Fatalf("side %d SACK=%v: %v", side, sack, err)
		}
		if err := s.TransportProtocolOption(tcp.ProtocolNumber, &rack); err != nil || rack != tcpip.TCPRACKLossDetection {
			t.Fatalf("side %d recovery=%v: %v", side, rack, err)
		}
		links[side] = channel.New(2048, 1500, "")
		if err := s.CreateNIC(1, links[side]); err != nil {
			t.Fatal(err)
		}
		if err := s.AddProtocolAddress(1, tcpip.ProtocolAddress{Protocol: ipv4.ProtocolNumber, AddressWithPrefix: addr[side].WithPrefix()}, stack.AddressProperties{}); err != nil {
			t.Fatal(err)
		}
		s.SetRouteTable([]tcpip.Route{{Destination: header.IPv4EmptySubnet, NIC: 1}})
	}
	var seq, high uint32
	var first, sawTarget bool
	bulk := !frontier
	var syn [2]int
	capture := func(side int, pkt *stack.PacketBuffer) {
		defer pkt.DecRef()
		view := pkt.ToView()
		defer view.Release()
		ip := header.IPv4(view.AsSlice())
		if ip.TransportProtocol() != tcp.ProtocolNumber {
			t.Fatalf("unexpected IP payload %d", ip.TransportProtocol())
		}
		seg := header.TCP(ip.Payload())
		opts := header.ParseTCPOptions(seg.Options())
		w := wire{At: int64(now()), From: side, Seq: seg.SequenceNumber(), ACK: seg.AckNumber(), Flags: seg.Flags(),
			Bytes: len(seg.Payload()), Hash: fmt.Sprintf("%x", sha256.Sum256(view.AsSlice())), TS: opts.TS,
			SACK: append([]header.SACKBlock(nil), opts.SACKBlocks...)}
		if w.Flags.Contains(header.TCPFlagSyn) {
			o := header.ParseSynOptions(seg.Options(), w.Flags.Contains(header.TCPFlagAck))
			w.Permitted, w.TS = o.SACKPermitted, o.TS
			if !w.Permitted {
				t.Fatalf("side %d SYN lacked SACK-permitted", side)
			}
			syn[side]++
		}
		if w.TS != frontier {
			t.Fatalf("side %d timestamp=%v, want %v: %+v", side, w.TS, frontier, w)
		}
		if side == 0 && w.Bytes != 0 && bulk {
			row.Offers++
			w.Offer = row.Offers
			if !first {
				seq, first, row.FirstData = w.Seq, true, int64(now())
				mu.Lock()
				base, haveBase = seq, true
				mu.Unlock()
			}
			offset := w.Seq - seq
			w.Retry = offset < high
			high = max(high, offset+uint32(w.Bytes))
			w.Dropped = loss.First > 0 && w.Offer >= loss.First && w.Offer <= loss.Last
			if loss.Name == "lost_repair" && w.Offer == 13 {
				w.Dropped = true
			}
			if w.Dropped {
				row.Drops++
			}
		}
		if frontier {
			w.IPBytes = pkt.Size()
			// Count every packet awaiting delivery, including propagation, as
			// netem does with its packet limit. Reject overflow before service.
			for _, p := range pending {
				if p.from == side {
					w.Occupancy++
				}
			}
			row.MaxOccupancy[side] = max(row.MaxOccupancy[side], w.Occupancy)
			if w.Occupancy >= 100 {
				w.QueueDrop, w.Dropped = true, true
				row.QueueDrops[side]++
				row.Drops++
				record(&w)
				return
			}
			// Injection below TCP costs the actual forward service and delay.
			// A retry can never trigger a second artificial loss.
			if bulk && side == 0 && w.Bytes != 0 && !sawTarget && uint32(frontierByte)-(w.Seq-seq) < uint32(w.Bytes) {
				sawTarget = true
				if loss.Name == "frontier_loss" {
					w.Injected, w.Dropped = true, true
					row.Drops++
				}
			}
		}
		wireRate := rates[side]
		tx[side] = max(tx[side], now()) + time.Duration((int64(pkt.Size())*int64(time.Second)+wireRate-1)/wireRate)
		at := tx[side] + latency
		w.Due = int64(at)
		record(&w)
		pending = append(pending, delivery{at, side, stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: pkt.ToBuffer()}), w})
	}
	fence := func() {
		t.Helper()
		for pass := 0; pass < 32; pass++ {
			gen := clock.callbacks.Load()
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
				if clock.callbacks.Load() != gen {
					return false
				}
				for side, l := range links {
					for p := l.Read(); p != nil; p = l.Read() {
						capture(side, p)
					}
				}
				return true
			}()
			if stable {
				return
			}
		}
		t.Fatal("TCP callbacks failed to settle")
	}
	step := func(deadline time.Duration) {
		fence()
		next := min(now()+tick, deadline)
		idx := -1
		for i, p := range pending {
			if p.at < next || (p.at == next && idx < 0) {
				next, idx = p.at, i
			}
		}
		for _, s := range stacks {
			s.Pause()
		}
		clock.Advance(next - now())
		for _, s := range stacks {
			s.Resume()
		}
		fence()
		if idx >= 0 {
			p := pending[idx]
			pending = append(pending[:idx], pending[idx+1:]...)
			if !p.row.Dropped {
				if frontier {
					mu.Lock()
					if !windowDone {
						row.Events = append(row.Events, event{Ingress: &frontierIngress{int64(now()), p.row}})
					}
					mu.Unlock()
				}
				links[1-p.from].InjectInbound(ipv4.ProtocolNumber, p.packet)
				if p.from == 1 && first && p.row.Flags.Contains(header.TCPFlagAck) {
					row.Acknowledged = max(row.Acknowledged, min(high, p.row.ACK-seq))
				}
			}
			p.packet.DecRef()
		}
		fence()
	}
	endpoint := func(side int) tcpip.Endpoint {
		var q waiter.Queue
		ep, err := stacks[side].NewEndpoint(tcp.ProtocolNumber, ipv4.ProtocolNumber, &q)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(ep.Close)
		return ep
	}
	client, listener := endpoint(0), endpoint(1)
	client.SocketOptions().SetSendBufferSize(buffer, true)
	listener.SocketOptions().SetReceiveBufferSize(buffer, true)
	if got := client.SocketOptions().GetSendBufferSize(); got != buffer {
		t.Fatalf("send buffer=%d", got)
	}
	if got := listener.SocketOptions().GetReceiveBufferSize(); got != buffer {
		t.Fatalf("receive buffer=%d", got)
	}
	serverAddr := tcpip.FullAddress{Addr: addr[1], Port: 8080}
	if err := listener.Bind(serverAddr); err != nil {
		t.Fatal(err)
	}
	if err := listener.Listen(2); err != nil {
		t.Fatal(err)
	}
	if err := client.Bind(tcpip.FullAddress{Addr: addr[0], Port: 40000}); err != nil {
		t.Fatal(err)
	}
	if _, ok := client.Connect(serverAddr).(*tcpip.ErrConnectStarted); !ok {
		t.Fatal("active open did not start")
	}
	var peer tcpip.Endpoint
	for peer == nil && now() < time.Second {
		step(time.Second)
		ep, _, err := listener.Accept(nil)
		if _, waiting := err.(*tcpip.ErrWouldBlock); waiting {
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		peer = ep
	}
	if peer == nil || syn != [2]int{1, 1} || tcp.EndpointState(client.State()) != tcp.StateEstablished {
		t.Fatalf("handshake did not establish SACK/no TS: %v", syn)
	}
	t.Cleanup(peer.Close)
	var actual tcpip.CongestionControlOption
	if err := client.GetSockOpt(&actual); err != nil || string(actual) != algorithm {
		t.Fatalf("sender CC=%s, want %s: %v", actual, algorithm, err)
	}
	row.Algorithm = string(actual)
	if frontier {
		client.SocketOptions().SetDelayOption(false)
		peer.SocketOptions().SetDelayOption(false)
		exchange := func(sender, receiver tcpip.Endpoint) {
			t.Helper()
			if n, err := sender.Write(bytes.NewReader([]byte{'p'}), tcpip.WriteOptions{}); err != nil || n != 1 {
				t.Fatalf("echo write=%d,%v", n, err)
			}
			limit := now() + time.Second
			var b bytes.Buffer
			for now() < limit {
				step(limit)
				_, err := receiver.Read(&b, tcpip.ReadOptions{})
				if _, wait := err.(*tcpip.ErrWouldBlock); wait {
					continue
				}
				if err != nil || b.String() != "p" {
					t.Fatalf("echo read=%q,%v", b.String(), err)
				}
				return
			}
			t.Fatal("echo timeout")
		}
		for range 7 {
			start := now()
			exchange(client, peer)
			exchange(peer, client)
			row.EchoNS = append(row.EchoNS, int64(now()-start))
		}
		bulk = true
	}
	data := make([]byte, demand)
	for i := range data {
		data[i] = byte(i*17 + i/251)
	}
	n, err := client.Write(bytes.NewReader(data), tcpip.WriteOptions{})
	if err != nil || n != int64(len(data)) {
		t.Fatalf("bulk Write=%d,%v", n, err)
	}
	row.Accepted = int(n)
	if err := client.Shutdown(tcpip.ShutdownWrite); err != nil {
		t.Fatal(err)
	}
	fence()
	if !first {
		t.Fatal("no first data offer")
	}
	deadline := time.Duration(row.FirstData) + horizon
	var received bytes.Buffer
	for {
		step(deadline)
		_, err := peer.Read(&received, tcpip.ReadOptions{})
		switch err.(type) {
		case nil, *tcpip.ErrWouldBlock:
		case *tcpip.ErrClosedForReceive:
			row.EOF = true
		default:
			t.Fatal(err)
		}
		fence()
		row.FinACK = tcp.EndpointState(client.State()) == tcp.StateFinWait2
		if row.EOF && row.FinACK && len(pending) == 0 {
			break
		}
		if now() == deadline {
			due := false
			for _, p := range pending {
				due = due || p.at <= deadline
			}
			if !due {
				break
			}
		}
	}
	row.End, row.Delivered, row.Pending = int64(now()), received.Len(), len(pending)
	if row.Delivered > len(data) || !bytes.Equal(received.Bytes(), data[:row.Delivered]) {
		t.Fatalf("incorrect payload at %d bytes", row.Delivered)
	}
	row.BodyHash = fmt.Sprintf("%x", sha256.Sum256(received.Bytes()))
	stats := stacks[0].Stats().TCP
	row.Retransmissions, row.RTOs, row.SACKRecoveries = stats.Retransmits.Value(), stats.Timeouts.Value(), stats.SACKRecovery.Value()
	if frontier {
		row.WireHash = fmt.Sprintf("%x", wireHash.Sum(nil))
	}
	if row.FinACK && row.Acknowledged != uint32(demand) {
		t.Fatal("FIN ACK without all bytes")
	}
	for side, s := range stacks {
		if s.Stats().TCP.ChecksumErrors.Value() != 0 || s.Stats().TCP.SegmentSendErrors.Value() != 0 {
			t.Fatalf("side %d link error", side)
		}
	}
	encoded, e := json.Marshal(row)
	if e != nil {
		t.Fatal(e)
	}
	const size = 2400
	chunks := (len(encoded) + size - 1) / size
	for i := range chunks {
		t.Logf("VIRTUAL_TCP_PART %s %d/%d %s", row.Case, i, chunks, encoded[i*size:min((i+1)*size, len(encoded))])
	}
	return row
}

// TestVirtualTCPRTOFlight holds all network and application inputs fixed while
// the remote driver changes the source commit. No reporter guard is activated.
func TestVirtualTCPRTOFlight(t *testing.T) {
	if runtime.GOMAXPROCS(0) != 1 {
		t.Fatal("one TCP processor per stack is required")
	}
	for _, schedule := range []lossSchedule{{"blackout", 1, 11}, {"partial", 2, 8}, {"lost_repair", 1, 11}} {
		t.Run(schedule.Name, func(t *testing.T) {
			runTrial(t, "reno", schedule, false, false)
		})
	}
}

// TestVirtualTCPSingleLossFrontier shares the real sender, receiver and
// deterministic clock with the existing loss diagnostics. Every arm uses the
// same finite queue; only receipt of the named original segment changes.
func TestVirtualTCPSingleLossFrontier(t *testing.T) {
	if runtime.GOMAXPROCS(0) != 1 {
		t.Fatal("one TCP processor per stack is required")
	}
	for _, name := range []string{"frontier_control", "frontier_loss"} {
		for _, cc := range []string{"cubic", "reno"} {
			t.Run(cc+"_"+name, func(t *testing.T) {
				runTrial(t, cc, lossSchedule{Name: name}, false, false)
			})
		}
	}
}
