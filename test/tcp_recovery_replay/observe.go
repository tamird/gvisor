package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
)

type span struct{ Start, End int }
type timedSpan struct {
	span
	ms int64
}
type observation struct {
	attempts, losses                                                []timedSpan
	mu                                                              sync.Mutex
	t0                                                              time.Time
	base                                                            uint32
	beforeS, beforeC                                                map[string]int64
	spans                                                           []span
	highestSent, sent, delivered, dropped, acks, zeroWindows        int
	lastACK, maxACK, lastWindow                                     int
	lastSentMS, lastDeliveredMS, lastACKMS, firstDropMS, lastDropMS int64
	lastSACK                                                        []span
	overflow                                                        bool
}

var currentObservation atomic.Pointer[observation]
var writerCalls, writerBytes, writerRequested, writerAttempted, writerErrors atomic.Int64

func endpointStates(h *gvisorHost) map[string]int64 {
	for _, registered := range h.stack.RegisteredEndpoints() {
		if e, ok := registered.(*tcp.Endpoint); ok {
			if state := e.ReplayState(); state != nil && state["remote_port"] != 0 {
				return state
			}
		}
	}
	return nil
}

func observeStart(t0 time.Time, server, client *gvisorHost) {
	serverState, clientState := endpointStates(server), endpointStates(client)
	if serverState == nil || clientState == nil {
		panic("connected loopback endpoint missing")
	}
	currentObservation.Store(&observation{t0: t0, base: uint32(serverState["nxt"]), beforeS: serverState, beforeC: clientState,
		lastACK: -1, maxACK: -1, lastWindow: -1, firstDropMS: -1, lastDropMS: -1})
}

func recordObservation(kind string, now time.Time, p *packetInfo) {
	o := currentObservation.Load()
	if o == nil || !p.isTCP {
		return
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	ms := now.Sub(o.t0).Milliseconds()
	if p.src == serverAddr && p.payloadLen > 0 {
		start := int(int32(p.seq - o.base))
		end := start + p.payloadLen
		switch kind {
		case "sent":
			if len(o.attempts) < 200000 {
				o.attempts = append(o.attempts, timedSpan{span{start, end}, ms})
			} else {
				o.overflow = true
			}
			o.sent++
			o.lastSentMS = ms
			o.highestSent = max(o.highestSent, end)
		case "drop":
			if o.dropped == 0 {
				o.firstDropMS = ms
			}
			if len(o.losses) < 200000 {
				o.losses = append(o.losses, timedSpan{span{start, end}, ms})
			} else {
				o.overflow = true
			}
			o.dropped++
			o.lastDropMS = ms
		case "delivered":
			o.delivered++
			o.lastDeliveredMS = ms
			if len(o.spans) < 200000 {
				o.spans = append(o.spans, span{start, end})
			} else {
				o.overflow = true
			}
		}
	} else if kind == "delivered" && p.dst == serverAddr && p.payloadLen == 0 {
		o.acks++
		o.lastACKMS = ms
		o.lastACK = int(int32(p.ack - o.base))
		o.maxACK = max(o.maxACK, o.lastACK)
		o.lastWindow = int(p.wnd)
		if p.wnd == 0 {
			o.zeroWindows++
		}
		o.lastSACK = o.lastSACK[:0]
		for _, b := range p.sackBlocks {
			o.lastSACK = append(o.lastSACK, span{int(int32(uint32(b.Start) - o.base)), int(int32(uint32(b.End) - o.base))})
		}
	}
}

type missingPart struct {
	span
	sent bool
}

// Partition the requested interval at every boundary observed on either side
// of the link. A scripted drop can border a suffix the sender never emitted.
func wireCoverage(size int, attempts []timedSpan, delivered []span) (neverSent, sentMissing int, first []missingPart) {
	type boundary struct{ pos, sends, arrivals int }
	points := []boundary{{pos: 0}, {pos: size}}
	add := func(r span, sends, arrivals int) {
		start, end := max(0, r.Start), min(size, r.End)
		if start < end {
			points = append(points, boundary{start, sends, arrivals}, boundary{end, -sends, -arrivals})
		}
	}
	for _, r := range attempts {
		add(r.span, 1, 0)
	}
	for _, r := range delivered {
		add(r, 0, 1)
	}
	sort.Slice(points, func(i, j int) bool { return points[i].pos < points[j].pos })
	pos, sends, arrivals := 0, 0, 0
	for _, p := range points {
		if pos < p.pos && arrivals == 0 {
			sent := sends > 0
			if sent {
				sentMissing += p.pos - pos
			} else {
				neverSent += p.pos - pos
			}
			if n := len(first); n > 0 && first[n-1].sent == sent && first[n-1].End == pos {
				first[n-1].End = p.pos
			} else if n < 8 {
				first = append(first, missingPart{span{pos, p.pos}, sent})
			}
		}
		sends += p.sends
		arrivals += p.arrivals
		pos = p.pos
	}
	return
}

func observeFinish(server, client *gvisorHost, n int, operation string, r fetchResult) {
	o := currentObservation.Swap(nil)
	s, c := endpointStates(server), endpointStates(client)
	o.mu.Lock()
	defer o.mu.Unlock()
	neverSent, sentMissing, gaps := wireCoverage(*sizeFlag, o.attempts, o.spans)
	missing := neverSent + sentMissing
	var gapHistory []map[string]int64
	for _, g := range gaps {
		h := map[string]int64{"start": int64(g.Start), "end": int64(g.End), "sent": 0, "script_drops": 0, "transmitted": 0}
		if g.sent {
			h["transmitted"] = 1
		}
		for name, ranges := range map[string][]timedSpan{"sent": o.attempts, "script_drops": o.losses} {
			for _, r := range ranges {
				if r.End > g.Start && r.Start < g.End {
					if h[name] == 0 {
						h["first_"+name+"_ms"] = r.ms
					}
					h[name]++
					h["last_"+name+"_ms"] = r.ms
				}
			}
		}
		gapHistory = append(gapHistory, h)
	}
	observedRTO := make([]int64, len(r.rtoTimes))
	for i, d := range r.rtoTimes {
		observedRTO[i] = d.Milliseconds()
	}
	e := ""
	if r.err != nil {
		e = r.err.Error()
	}
	var networkError net.Error
	timedOut := errors.As(r.err, &networkError) && networkError.Timeout()
	out := map[string]any{"operation": operation, "client_timed_out": timedOut, "never_sent_bytes": neverSent, "sent_undelivered_bytes": sentMissing, "sender_rto_delta": r.stats.timeouts, "sender_retransmits_delta": r.stats.retransmits, "seed": *seedFlag, "loss": *lossFlag, "requested": *sizeFlag, "client_read": n, "client_error": e,
		"writer_requests": writerCalls.Load(), "server_requested": writerRequested.Load(), "server_attempted": writerAttempted.Load(), "server_written": writerBytes.Load(), "server_errors": writerErrors.Load(),
		"ms": r.elapsed.Milliseconds(), "server_before": o.beforeS, "peer_before": o.beforeC, "server_after": s, "peer_after": c,
		"from_server_seq": o.base, "highest_sent_offset": o.highestSent, "last_ack_offset": o.lastACK, "max_ack_offset": o.maxACK, "last_raw_window": o.lastWindow, "last_sack": o.lastSACK,
		"sent_packets": o.sent, "delivered_packets": o.delivered, "dropped_packets": o.dropped, "queue_drops": r.queueDrop, "ack_packets": o.acks, "zero_window_acks": o.zeroWindows,
		"first_drop_ms": o.firstDropMS, "last_drop_ms": o.lastDropMS, "last_sent_ms": o.lastSentMS, "last_data_delivered_ms": o.lastDeliveredMS, "last_ack_ms": o.lastACKMS,
		"missing_wire_bytes": missing, "first_wire_gaps": gapHistory, "capture_limit": o.overflow, "observed_rtos_ms": observedRTO}
	data, err := json.Marshal(out)
	if err != nil {
		panic(err)
	}
	fmt.Printf("TCP_REPLAY_RESULT %s\n", data)
}
