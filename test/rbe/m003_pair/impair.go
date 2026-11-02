// Fork-only link schedule for the M003 real two-stack transfer.
package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"flag"
	"os"
	"sort"
	"sync"
	"time"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
)

type ByteRange struct{ Offset, Length uint32 }
type Signal struct {
	Kind            string
	At              float64
	Head, SACKRight uint32
	Lost            bool
	RTO             float64
	Stamp           int64
	Blocks          [][2]uint32
	Hole            *Hole
}

type Hole struct {
	Start, End, LatestEnd, Count                     uint32
	AlreadyLost                                      bool
	HeadAge, HoleAge, LatestAge, RTT, Reo, Precision float64
}

var reference = flag.String("ranges", "", "stock screen's eight relative byte ranges")
var x struct {
	sync.Mutex
	start, anchor   time.Time
	origin          uint32
	armed           bool
	Reference, Seen []ByteRange
	Signals         []Signal
}

func startExperiment(r *Recorder) error {
	x.start = r.start
	if *reference != "" {
		b, err := os.ReadFile(*reference)
		if err != nil {
			return err
		}
		if err := json.Unmarshal(b, &x.Reference); err != nil {
			return err
		}
	}
	tcp.M003RecoveryObservation = func(kind string, una uint32, blocks []header.SACKBlock, stamp int64, lost bool, rto time.Duration) {
		x.Lock()
		defer x.Unlock()
		s := Signal{Kind: kind, At: time.Since(x.start).Seconds(), Head: una, Stamp: stamp, Lost: lost, RTO: rto.Seconds()}
		for _, b := range blocks {
			s.Blocks = append(s.Blocks, [2]uint32{uint32(b.Start), uint32(b.End)})
		}
		if len(s.Blocks) != 0 {
			s.SACKRight = s.Blocks[0][1]
		}
		x.Signals = append(x.Signals, s)
	}
	tcp.M003RACKLoss = func(head, start, end, latestEnd, count uint32, stamp int64, alreadyLost bool, headAge, holeAge, latestAge, rtt, reo, precision time.Duration) {
		x.Lock()
		defer x.Unlock()
		x.Signals = append(x.Signals, Signal{Kind: "rack-loss", At: time.Since(x.start).Seconds(), Head: head, Stamp: stamp,
			Hole: &Hole{Start: start, End: end, LatestEnd: latestEnd, Count: count, AlreadyLost: alreadyLost,
				HeadAge: headAge.Seconds(), HoleAge: holeAge.Seconds(), LatestAge: latestAge.Seconds(), RTT: rtt.Seconds(), Reo: reo.Seconds(), Precision: precision.Seconds()}})
	}
	return nil
}
func finishExperiment(file string) error {
	x.Lock()
	defer x.Unlock()
	b, err := json.Marshal(struct {
		Origin          uint32
		Anchor          float64
		Reference, Seen []ByteRange
		Signals         []Signal
	}{x.origin, x.anchor.Sub(x.start).Seconds(), x.Reference, x.Seen, x.Signals})
	if err != nil {
		return err
	}
	return os.WriteFile(file+".events.json", b, 0600)
}

func impaired(due time.Time, scenario string, seq uint32, n, position, copyNo int) (time.Time, bool) {
	x.Lock()
	defer x.Unlock()
	if position == 1 && copyNo == 1 {
		x.origin = seq
	}
	r := ByteRange{seq - x.origin, uint32(n)}
	slot := -1
	if len(x.Reference) == 0 {
		if position < 101 {
			return due, false
		}
		slot = position - 101
	} else {
		if r.Offset < x.Reference[0].Offset {
			return due, false
		}
		slot = 8
		for i, named := range x.Reference {
			if r == named {
				slot = i
				break
			}
		}
	}
	if slot < 8 && copyNo == 1 {
		x.Seen = append(x.Seen, r)
	}
	if slot == 0 && copyNo == 1 {
		x.anchor = due.Add(-5 * time.Millisecond)
		x.armed = true
	}
	if !x.armed {
		return due, false
	}
	if slot < 3 && copyNo == 1 {
		return due, true
	}
	clear := 265 * time.Millisecond
	if scenario == "held" {
		clear = 495 * time.Millisecond
	}
	delay := clear
	switch slot {
	case 3, 4, 5:
		delay = 130 * time.Millisecond
	case 6:
		delay = 225 * time.Millisecond
	case 7:
		if scenario == "held" {
			delay = 365 * time.Millisecond
		}
	}
	// Only this original packet is released earlier than in the measured held schedule.
	if scenario == "held" && r == (ByteRange{140672, 1200}) && copyNo == 1 {
		delay = 280 * time.Millisecond
	}
	lower := x.anchor.Add(delay)
	if due.Before(lower) {
		due = lower
	}
	return due, false
}

// Keep normal packets FIFO and allow only the explicitly delayed ranges to reorder.
func deliver(ctx context.Context, q <-chan Transfer, to *channel.Endpoint) {
	var pending []Transfer
	for {
		if len(pending) > 0 && !time.Now().Before(pending[0].due) {
			p := pending[0]
			pending = pending[1:]
			pkt := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: buffer.MakeWithData(p.data)})
			to.InjectInbound(ipv4.ProtocolNumber, pkt)
			pkt.DecRef()
			continue
		}
		var wake <-chan time.Time
		var t *time.Timer
		if len(pending) > 0 {
			t = time.NewTimer(time.Until(pending[0].due))
			wake = t.C
		}
		select {
		case <-ctx.Done():
			if t != nil {
				t.Stop()
			}
			return
		case p, ok := <-q:
			if t != nil {
				t.Stop()
			}
			if !ok {
				return
			}
			pending = append(pending, p)
			if n := len(pending); n > 1 && p.due.Before(pending[n-2].due) {
				sort.SliceStable(pending, func(i, j int) bool { return pending[i].due.Before(pending[j].due) })
			}
		case <-wake:
		}
	}
}

func sackBlocks(t []byte) [][2]uint32 {
	var blocks [][2]uint32
	for i := 20; i < len(t); {
		switch t[i] {
		case 0:
			return blocks
		case 1:
			i++
			continue
		}
		if i+2 > len(t) || t[i+1] < 2 || int(t[i+1]) > len(t)-i {
			break
		}
		end := i + int(t[i+1])
		if t[i] == 5 {
			for j := i + 2; j+8 <= end; j += 8 {
				blocks = append(blocks, [2]uint32{binary.BigEndian.Uint32(t[j:]), binary.BigEndian.Uint32(t[j+4:])})
			}
		}
		i = end
	}
	return blocks
}
