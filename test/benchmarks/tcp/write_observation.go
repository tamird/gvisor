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
	"io"
	"log"
	"maps"
	"net"
	"os"
	"runtime"
	"sync"

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
	n, err := w.conn.Write(p)
	w.mu.Lock()
	w.Requested[len(p)]++
	w.Accepted[n]++
	if err != nil {
		w.WriteErrors++
	}
	w.mu.Unlock()
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
	// Hide WriterTo so io.CopyBuffer actually uses the selected buffer.
	_, err := io.CopyBuffer(w, struct{ io.Reader }{src}, make([]byte, *copyBuffer))
	w.mu.Lock()
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
			"write_errors": w.WriteErrors, "copy_error": w.CopyError, "completed": w.Completed}
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
				"sack_recovery": t.SACKRecovery.Value(), "tlp_recovery": t.TLPRecovery.Value(),
				"timeouts": t.Timeouts.Value(), "fast_retransmit": t.FastRetransmit.Value()}
		}
	}
	if err := json.NewEncoder(f).Encode(map[string]any{"buffer": *copyBuffer, "writes": rows, "stack": stats, "gomaxprocs": runtime.GOMAXPROCS(0), "num_cpu": runtime.NumCPU(), "gso_max": *gso, "swgso": *swgso, "gro": *gro}); err != nil {
		log.Fatal(err)
	}
}
