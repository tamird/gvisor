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
	"encoding/json"
	"flag"
	"fmt"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"io"
	"log"
	"maps"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
)

var copyBuffer = flag.Int("copy_buffer", 0, "fork observation: explicit forwarding buffer size")
var writeReport = flag.String("write_report", "", "fork observation: accepted Write histogram")
var copiesMu sync.Mutex
var copies []*observedCopy

type observedCopy struct {
	conn        net.Conn
	phase       atomic.Uint32
	bytes       atomic.Uint64
	readBytes   atomic.Uint64
	Samples     []copySample
	mu          sync.Mutex
	To          string
	Local       string
	Remote      string
	Requested   map[int]uint64
	Accepted    map[int]uint64
	WriteErrors uint64
	CopyError   string
	Completed   bool
}

func (w *observedCopy) Write(p []byte) (int, error) {
	w.phase.Store(2) // write pending
	n, err := w.conn.Write(p)
	w.bytes.Add(uint64(n))
	w.phase.Store(0) // neither syscall is pending
	w.mu.Lock()
	w.Requested[len(p)]++
	w.Accepted[n]++
	if err != nil {
		w.WriteErrors++
	}
	w.mu.Unlock()
	return n, err
}

type observedReader struct {
	src io.Reader
	w   *observedCopy
}

func (r observedReader) Read(p []byte) (int, error) {
	r.w.phase.Store(1)
	n, err := r.src.Read(p)
	r.w.readBytes.Add(uint64(n))
	r.w.phase.Store(0)
	return n, err
}

func copyObserved(dst, src net.Conn) {
	if *copyBuffer == 0 {
		io.Copy(io.MultiWriter(dst), io.MultiReader(src))
		return
	}
	w := &observedCopy{
		conn: dst, To: fmt.Sprintf("%T", dst),
		Local: dst.LocalAddr().String(), Remote: dst.RemoteAddr().String(),
		Requested: make(map[int]uint64), Accepted: make(map[int]uint64),
	}
	copiesMu.Lock()
	copies = append(copies, w)
	copiesMu.Unlock()
	if c, ok := dst.(*gonet.TCPConn); ok && *server {
		go sampleCopy(w, c)
	}
	// Hide WriterTo so io.CopyBuffer actually uses the selected buffer.
	_, err := io.CopyBuffer(w, observedReader{src, w}, make([]byte, *copyBuffer))
	w.mu.Lock()
	w.phase.Store(3)
	w.Completed = true
	if err != nil {
		w.CopyError = err.Error()
	}
	w.mu.Unlock()
}

func writeCopyReport(in, out impl) {
	if *writeReport == "" {
		return
	}
	f, err := os.Create(*writeReport)
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close()
	rows := []map[string]any{}
	copiesMu.Lock()
	defer copiesMu.Unlock()
	for _, w := range copies {
		w.mu.Lock()
		row := map[string]any{"to": w.To, "local": w.Local, "remote": w.Remote,
			"requested": maps.Clone(w.Requested), "accepted": maps.Clone(w.Accepted),
			"write_errors": w.WriteErrors, "copy_error": w.CopyError, "completed": w.Completed, "samples": w.Samples}
		if c, ok := w.conn.(*net.TCPConn); ok {
			if raw, err := c.SyscallConn(); err == nil {
				raw.Control(func(fd uintptr) {
					if ti, err := unix.GetsockoptTCPInfo(int(fd), unix.IPPROTO_TCP, unix.TCP_INFO); err == nil {
						row["tcp_info"] = map[string]any{"snd_mss": ti.Snd_mss, "options": ti.Options,
							"total_retrans": ti.Total_retrans, "bytes_retrans": ti.Bytes_retrans,
							"data_segs_out": ti.Data_segs_out, "bytes_acked": ti.Bytes_acked}
					}
				})
			}
		}
		w.mu.Unlock()
		rows = append(rows, row)
	}
	stats := map[string]any{}
	for name, v := range map[string]impl{"in": in, "out": out} {
		if n, ok := v.(netstackImpl); ok {
			t := n.s.Stats().TCP
			var sackOpt tcpip.TCPSACKEnabled
			var recovery tcpip.TCPRecovery
			var cc tcpip.CongestionControlOption
			for _, opt := range []tcpip.GettableTransportProtocolOption{&sackOpt, &recovery, &cc} {
				if err := n.s.TransportProtocolOption(tcp.ProtocolNumber, opt); err != nil {
					log.Fatalf("option readback: %v", err)
				}
			}
			stats[name] = map[string]any{"sack": sackOpt, "recovery": recovery, "cc": cc, "segments_sent": t.SegmentsSent.Value(),
				"retransmits": t.Retransmits.Value(), "fast_recovery": t.FastRecovery.Value(),
				"sack_recovery": t.SACKRecovery.Value(), "valid_rx": t.ValidSegmentsReceived.Value(), "invalid_rx": t.InvalidSegmentsReceived.Value(), "send_errors": t.SegmentSendErrors.Value(), "checksum_errors": t.ChecksumErrors.Value(), "dsacks": t.SegmentsAckedWithDSACK.Value(), "tlp_recovery": t.TLPRecovery.Value(),
				"timeouts": t.Timeouts.Value(), "fast_retransmit": t.FastRetransmit.Value()}
		}
	}
	eventMu.Lock()
	defer eventMu.Unlock()
	ordered := slices.Clone(events)
	if lostEvents != 0 {
		start := 512 + lostEvents%512
		ordered = append(ordered[:512], events[start:]...)
		ordered = append(ordered, events[512:start]...)
	}
	if err := json.NewEncoder(f).Encode(map[string]any{"link": linkSnapshot(), "events": ordered, "events_dropped": lostEvents, "buffer": *copyBuffer, "writes": rows, "stack": stats, "gomaxprocs": runtime.GOMAXPROCS(0), "num_cpu": runtime.NumCPU(), "gso_max": *gso, "swgso": *swgso, "gro": *gro}); err != nil {
		log.Fatal(err)
	}
}

type copySample struct {
	At       int64
	Phase    uint32
	Returned uint64
	Read     uint64
	Sender   tcp.ResearchState
}

var eventMu sync.Mutex
var events []tcp.ResearchState
var lostEvents int

func keepEvent(e tcp.ResearchState) {
	eventMu.Lock()
	defer eventMu.Unlock()
	if len(events) == 1024 {
		events[512+lostEvents%512] = e
		lostEvents++
		return
	}
	events = append(events, e)
}

func sampleCopy(w *observedCopy, c *gonet.TCPConn) {
	ep := c.ResearchEndpoint().(*tcp.Endpoint)
	// The packet capture is normally asleep. Arm it only when this bulk copy
	// has delivered at least 1 MiB to netstack and stops returning writes.
	tick := time.NewTicker(200 * time.Millisecond)
	defer tick.Stop()
	var previous uint64
	idle := 0
	triggered := false
	for i := 0; i < 180; i++ {
		<-tick.C
		n := w.bytes.Load()
		if n < 1<<20 {
			if w.phase.Load() == 3 {
				return
			}
			continue
		}
		phase := w.phase.Load()
		sample := copySample{At: time.Now().UnixNano(), Phase: phase, Returned: n, Read: w.readBytes.Load(), Sender: ep.ResearchSample()}
		w.mu.Lock()
		w.Samples = append(w.Samples, sample)
		w.mu.Unlock()
		if sample.Sender.Why == "no_sender" || phase == 3 {
			return
		}
		if n == previous {
			idle++
		} else {
			idle = 0
		}
		previous = n
		if idle >= 4 && !triggered {
			port := int(sample.Sender.Remote)
			if port == 0 {
				continue
			}
			if err := os.WriteFile(filepath.Join(filepath.Dir(*writeReport), "stalled-port"), []byte(strconv.Itoa(port)+"\n"), 0644); err != nil {
				log.Printf("writing capture trigger: %v", err)
			}
			triggered = true
		}
	}
}
