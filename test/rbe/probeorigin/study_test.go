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

package main

import (
	"bytes"
	"crypto/sha256"
	"fmt"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/waiter"
)

type zeroReader struct{}

func (zeroReader) Read(b []byte) (int, error) { clear(b); return len(b), nil }

type observation struct {
	ackHash                [32]byte
	probeAt, recvAt, ackAt time.Duration
	copies, timeouts       uint64
}

func TestProbeOrigin(t *testing.T) {
	for _, cc := range []string{"reno", "cubic"} {
		for _, ts := range []bool{false, true} {
			for _, expired := range []bool{false, true} {
				var first observation
				for _, origin := range []string{"original", "probe"} {
					t.Run(fmt.Sprintf("%s/ts%t/expired%t/%s", cc, ts, expired, origin), func(t *testing.T) {
						synctest.Test(t, func(t *testing.T) {
							actual := probeOrigin(t, cc, ts, expired, origin)
							if origin == "original" {
								first = actual
							} else if actual != first {
								t.Fatalf("different ACK/times/sender response: original=%+v probe=%+v", first, actual)
							}
						})
					})
				}
			}
		}
	}
}

// The link and receiver are real in both histories. It discards one copy and
// delivers the other at the same virtual instant; it never constructs the SACK.
func probeOrigin(t *testing.T, cc string, ts, expired bool, origin string) observation {
	t.Helper()
	started := time.Now()
	addr := [2]tcpip.Address{tcpip.AddrFrom4([4]byte{10, 0, 0, 1}), tcpip.AddrFrom4([4]byte{10, 0, 0, 2})}
	var stacks [2]*stack.Stack
	var links [2]*channel.Endpoint
	var eps []tcpip.Endpoint
	defer func() {
		for _, s := range stacks {
			if s != nil {
				s.Resume()
			}
		}
		for i := len(eps) - 1; i >= 0; i-- {
			eps[i].Close()
		}
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
	}()
	for i := range stacks {
		s := stack.New(stack.Options{NetworkProtocols: []stack.NetworkProtocolFactory{ipv4.NewProtocol}, TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol}, Clock: tcpip.NewStdClock(), SecureRNG: zeroReader{}})
		stacks[i] = s
		links[i] = channel.New(64, 1500, "")
		if e := s.CreateNIC(1, links[i]); e != nil {
			t.Fatal(e)
		}
		if e := s.AddProtocolAddress(1, tcpip.ProtocolAddress{Protocol: ipv4.ProtocolNumber, AddressWithPrefix: addr[i].WithPrefix()}, stack.AddressProperties{}); e != nil {
			t.Fatal(e)
		}
		s.SetRouteTable([]tcpip.Route{{Destination: header.IPv4EmptySubnet, NIC: 1}})
		sack := tcpip.TCPSACKEnabled(true)
		rack := tcpip.TCPRecovery(tcpip.TCPRACKLossDetection)
		if e := s.SetTransportProtocolOption(tcp.ProtocolNumber, &sack); e != nil {
			t.Fatal(e)
		}
		if e := s.SetTransportProtocolOption(tcp.ProtocolNumber, &rack); e != nil {
			t.Fatal(e)
		}
		s.Pause()
	}
	freeze := func(i int) {
		now := time.Now()
		stacks[i].Resume()
		stacks[i].Pause()
		synctest.Wait()
		if time.Now() != now {
			t.Fatal("dispatcher advanced time")
		}
	}
	advance := func(d time.Duration) {
		now := time.Now()
		synctest.Wait()
		time.Sleep(d)
		synctest.Wait()
		if time.Since(now) != d {
			t.Fatalf("clock advanced %v, want %v", time.Since(now), d)
		}
	}
	read := func(i int) []byte {
		t.Helper()
		p := links[i].Read()
		if p == nil {
			t.Fatalf("missing packet on link %d at %s", i, time.Since(started))
		}
		defer p.DecRef()
		v := p.ToView()
		defer v.Release()
		return bytes.Clone(v.AsSlice())
	}
	seg := func(p []byte) header.TCP { return header.TCP(header.IPv4(p).Payload()) }
	inject := func(i int, p []byte) {
		t.Helper()
		in := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: buffer.MakeWithData(p)})
		defer in.DecRef()
		valid := stacks[1-i].Stats().TCP.ValidSegmentsReceived
		old := valid.Value()
		links[1-i].InjectInbound(ipv4.ProtocolNumber, in)
		freeze(1 - i)
		if n := valid.Value() - old; n != 1 {
			t.Fatalf("peer saw %d segments", n)
		}
	}
	endpoint := func(i int) tcpip.Endpoint {
		ep, e := stacks[i].NewEndpoint(tcp.ProtocolNumber, ipv4.ProtocolNumber, new(waiter.Queue))
		if e != nil {
			t.Fatal(e)
		}
		eps = append(eps, ep)
		return ep
	}
	listener := endpoint(1)
	remote := tcpip.FullAddress{Addr: addr[1], Port: 8080}
	if e := listener.Bind(remote); e != nil {
		t.Fatal(e)
	}
	if e := listener.Listen(2); e != nil {
		t.Fatal(e)
	}
	client := endpoint(0)
	if e := client.Bind(tcpip.FullAddress{Addr: addr[0], Port: 40000}); e != nil {
		t.Fatal(e)
	}
	if _, ok := client.Connect(remote).(*tcpip.ErrConnectStarted); !ok {
		t.Fatal("connect did not start")
	}
	syn := read(0)
	if !ts {
		stripSYNTimestamps(syn)
	}
	advance(5 * time.Millisecond)
	inject(0, syn)
	sa := read(1)
	opts := header.ParseSynOptions(seg(sa).Options(), true)
	if opts.TS != ts || !opts.SACKPermitted {
		t.Fatalf("SYN-ACK options=%+v, want TS=%t, SACK", opts, ts)
	}
	advance(5 * time.Millisecond)
	inject(1, sa)
	ack := read(0)
	advance(5 * time.Millisecond)
	inject(0, ack)
	server, _, err := listener.Accept(nil)
	if err != nil {
		t.Fatal(err)
	}
	eps = append(eps, server)
	alg := tcpip.CongestionControlOption(cc)
	if e := server.SetSockOpt(&alg); e != nil {
		t.Fatal(e)
	}
	alg = ""
	if e := server.GetSockOpt(&alg); e != nil || string(alg) != cc {
		t.Fatalf("cc=%v, err=%v", alg, e)
	}
	info := func() tcpip.TCPInfoOption {
		var inf tcpip.TCPInfoOption
		if e := server.GetSockOpt(&inf); e != nil {
			t.Fatal(e)
		}
		return inf
	}
	mss := int(info().SndMss)
	if mss < 1 {
		t.Fatal("no sender MSS")
	}
	write := func(s string) {
		t.Helper()
		if n, e := server.Write(strings.NewReader(s), tcpip.WriteOptions{}); e != nil || n != int64(len(s)) {
			t.Fatalf("write=%d,%v", n, e)
		}
		freeze(1)
	}
	prime := func(rtt time.Duration) {
		t.Helper()
		s := strings.Repeat("w", mss)
		write(s)
		p := read(1)
		if string(seg(p).Payload()) != s {
			t.Fatal("wrong full-size priming data")
		}
		advance(rtt / 2)
		inject(1, p)
		a := read(0)
		if seg(a).AckNumber() != seg(p).SequenceNumber()+uint32(mss) {
			t.Fatal("priming did not cumulatively ACK the full MSS")
		}
		advance(rtt / 2)
		inject(0, a)
		var got bytes.Buffer
		n, e := client.Read(&got, tcpip.ReadOptions{})
		if e != nil || n.Count != mss || got.String() != s {
			t.Fatalf("priming read=%v,%v", n, e)
		}
	}
	prime(10 * time.Millisecond)
	if expired {
		advance(6 * time.Minute)
	} else {
		advance(time.Second)
	}
	prime(30 * time.Millisecond)
	s := strings.Repeat("a", mss) + strings.Repeat("b", mss) + strings.Repeat("c", mss)
	write(s)
	flightAt := time.Since(started)
	first := read(1)
	second := read(1)
	original := read(1)
	hole := seg(first).SequenceNumber()
	tail := seg(original).SequenceNumber()
	if seg(second).SequenceNumber() != hole+uint32(mss) || tail != hole+uint32(2*mss) || string(seg(original).Payload()) != s[2*mss:] {
		t.Fatal("incomplete original flight")
	}
	// The first two data segments never reach the receiver. The original
	// third and its sole tail probe are both real sender packets.
	stats := stacks[1].Stats().TCP
	var probe []byte
	for probe == nil && time.Since(started)-flightAt < 150*time.Millisecond {
		advance(time.Millisecond)
		freeze(1)
		if p := links[1].Read(); p != nil {
			v := p.ToView()
			probe = bytes.Clone(v.AsSlice())
			v.Release()
			p.DecRef()
		}
	}
	obs := observation{probeAt: time.Since(started)}
	if probe == nil || seg(probe).SequenceNumber() != tail || !bytes.Equal(seg(probe).Payload(), seg(original).Payload()) || stats.Timeouts.Value() != 0 || stats.Retransmits.Value() != 1 {
		t.Fatalf("not a single pre-RTO tail probe; retx=%d timeouts=%d", stats.Retransmits.Value(), stats.Timeouts.Value())
	}
	advance(10 * time.Millisecond)
	obs.recvAt = time.Since(started)
	delivered := original
	if origin == "probe" {
		delivered = probe
	}
	inject(1, delivered)
	a := read(0)
	tcpACK := seg(a)
	o := tcpACK.ParsedOptions()
	if tcpACK.Flags() != header.TCPFlagAck || tcpACK.AckNumber() != hole || len(o.SACKBlocks) != 1 || uint32(o.SACKBlocks[0].Start) != tail || uint32(o.SACKBlocks[0].End) != tail+uint32(mss) || o.TS != ts {
		t.Fatalf("unexpected receiver SACK: %+v", o)
	}
	obs.ackHash = sha256.Sum256(tcpACK)
	advance(10 * time.Millisecond)
	obs.ackAt = time.Since(started)
	inject(0, a)
	advance(10 * time.Millisecond)
	freeze(1)
	obs.copies = stats.Retransmits.Value() - 1
	obs.timeouts = stats.Timeouts.Value()
	if obs.timeouts != 0 {
		t.Fatal("RTO before observation")
	}
	var repairs []uint32
	for p := links[1].Read(); p != nil; p = links[1].Read() {
		v := p.ToView()
		pkt := seg(v.AsSlice())
		repairs = append(repairs, pkt.SequenceNumber()-hole)
		v.Release()
		p.DecRef()
	}
	if len(repairs) != int(obs.copies) {
		t.Fatalf("repairs=%v counter=%d", repairs, obs.copies)
	}
	t.Logf("ORIGIN cc=%s ts=%t expired=%t delivered=%s lastack=%d tail=%d mss=%d original_at_ns=%d probe_at_ns=%d receiver_at_ns=%d sack_at_ns=%d original_ts=%d probe_ts=%d tsecr=%d ack_sha=%x additional_repairs=%v timeouts=%d", cc, ts, expired, origin, hole, tail, mss, flightAt, obs.probeAt, obs.recvAt, obs.ackAt, seg(original).ParsedOptions().TSVal, seg(probe).ParsedOptions().TSVal, o.TSEcr, obs.ackHash, repairs, obs.timeouts)
	return obs
}
