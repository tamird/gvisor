// Fork benchmark observations. The gVisor protocol implementation is unchanged.
package main

import (
	"encoding/json"
	"flag"
	"log"
	"net"
	"sync"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
)

var observation = flag.String("observation", "plain", "plain, historical empty callback, or measured")

func emit(fields map[string]any) {
	fields["ns"] = time.Now().UnixNano()
	b, err := json.Marshal(fields)
	if err != nil { log.Fatal(err) }
	log.Printf("WANSAMPLE %s", b)
}

// The historical benchmark already installed a full per-segment probe. Only
// measured trials turn it into samples, which are taken after ACK processing.
func observeNetstack() tcp.TCPProbeFunc {
	switch *observation {
	case "plain": return nil
	case "historical": return func(*tcp.TCPEndpointState) {}
	case "measured":
	default: log.Fatalf("unknown observation: %q", *observation)
	}
	var mu sync.Mutex
	last := make(map[uint16]time.Time)
	return func(s *tcp.TCPEndpointState) {
		if s.ID.RemotePort != 44000 && s.ID.RemotePort != 44001 { return }
		now := time.Now()
		mu.Lock()
		if now.Sub(last[s.ID.LocalPort]) < 250*time.Millisecond { mu.Unlock(); return }
		last[s.ID.LocalPort] = now
		mu.Unlock()
		x := s.Sender
		emit(map[string]any{"stack":"netstack", "port":s.ID.LocalPort, "peer":s.ID.RemotePort, "cwnd":x.SndCwnd, "mss":x.MaxPayloadSize, "wnd":x.SndWnd, "out":x.Outstanding, "sacked":x.SackedOut, "seq_flight":uint32(x.SndNxt-x.SndUna), "una":uint32(x.SndUna), "queued":s.SndBufState.SndBufUsed, "bufsize":s.SndBufState.SndBufSize, "recovery":x.FastRecovery.Active, "rtt_us":x.RTTState.SRTT.Microseconds()})
	}
}

// The native sender is the dialed proxy socket facing the bottleneck; neither
// the iperf socket on loopback nor the receiving proxy is a substitute.
func observeNative(c *net.TCPConn) {
	if *observation != "measured" { return }
	peer := c.RemoteAddr().(*net.TCPAddr).Port
	if peer != 44000 && peer != 44001 { return }
	port := c.LocalAddr().(*net.TCPAddr).Port
	raw, err := c.SyscallConn()
	if err != nil { log.Fatal(err) }
	go func() {
		tick := time.NewTicker(250*time.Millisecond)
		defer tick.Stop()
		for range tick.C {
			var info *unix.TCPInfo
			var sockErr error
			if err := raw.Control(func(fd uintptr) { info, sockErr = unix.GetsockoptTCPInfo(int(fd),unix.IPPROTO_TCP,unix.TCP_INFO) }); err != nil { return }
			if sockErr != nil { log.Fatal(sockErr) }
			emit(map[string]any{"stack":"linux", "port":port, "peer":peer, "cwnd":info.Snd_cwnd, "mss":info.Snd_mss, "wnd":info.Snd_wnd, "out":info.Unacked, "sacked":info.Sacked, "lost":info.Lost, "retrans":info.Total_retrans, "notsent":info.Notsent_bytes, "acked":info.Bytes_acked, "sent":info.Bytes_sent, "rwnd_us":info.Rwnd_limited, "sndbuf_us":info.Sndbuf_limited, "busy_us":info.Busy_time, "ca":info.Ca_state, "rtt_us":info.Rtt})
		}
	}()
}
