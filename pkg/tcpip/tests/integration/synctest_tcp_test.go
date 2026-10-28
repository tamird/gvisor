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

package synctest_tcp_test

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/waiter"
)

// Each probe runs in a child so its testing watchdog can report a bubble that
// never reaches quiescence. The parent collects the original stacks and packets.
func TestTCPVirtualSYN(t *testing.T) {
	const child = "TCP_SYNCTEST_CHILD"
	if mode := os.Getenv(child); mode != "" {
		if mode == "wall" {
			probe(t, mode)
		} else {
			synctest.Test(t, func(t *testing.T) { probe(t, mode) })
		}
		return
	}
	bin, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, mode := range []string{"wall", "plain", "paused"} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, bin, "-test.run=^TestTCPVirtualSYN$", "-test.timeout=5s", "-test.v")
			for _, pair := range os.Environ() {
				key, _, _ := strings.Cut(pair, "=")
				switch key {
				case "GO_TEST_WRAP", "GO_TEST_WRAP_TESTV", "XML_OUTPUT_FILE", "TESTBRIDGE_TEST_ONLY", "GOTRACEBACK":
				default:
					cmd.Env = append(cmd.Env, pair)
				}
			}
			cmd.Env = append(cmd.Env, child+"="+mode, "GOTRACEBACK=all")
			start := time.Now()
			out, err := cmd.CombinedOutput()
			elapsed := time.Since(start)
			log := string(out)
			t.Logf("mode=%s wall=%s exit=%v\n%s", mode, elapsed, err, log)
			if err == nil && strings.Contains(log, "SYN_RETRANSMITTED mode="+mode) {
				t.Logf("PROBE_RESULT %s retransmitted", mode)
				return
			}
			// Require evidence of this specific blocked state, not just a
			// nonzero exit, scheduler delay, or unsuccessful network setup.
			if mode != "wall" && err != nil && ctx.Err() == nil &&
				strings.Contains(log, "SYN_SENT mode="+mode) &&
				strings.Contains(log, "BEFORE_WAIT mode="+mode) &&
				!strings.Contains(log, "AFTER_WAIT mode="+mode) &&
				strings.Contains(log, "test timed out after 5s") &&
				strings.Contains(log, "testing/synctest.Wait") &&
				idleProcessorStack(log) {
				t.Logf("PROBE_RESULT %s blocked_wait", mode)
				return
			}
			t.Fatalf("unclassified probe: %v", err)
		})
	}
}

func idleProcessorStack(log string) bool {
	for _, trace := range strings.Split(log, "\n\n") {
		if strings.Contains(trace, "tcp.(*processor).start") &&
			strings.Contains(trace, "sleep.(*Sleeper).Fetch") {
			return true
		}
	}
	return false
}

type zeroReader struct{}

func (zeroReader) Read(p []byte) (int, error) { clear(p); return len(p), nil }

func probe(t *testing.T, mode string) {
	if runtime.GOMAXPROCS(0) != 1 {
		t.Fatalf("want one TCP processor, GOMAXPROCS=%d", runtime.GOMAXPROCS(0))
	}
	if mode != "wall" {
		start := time.Now()
		time.Sleep(2 * time.Second)
		if start.Year() != 2000 || time.Since(start) != 2*time.Second {
			t.Fatalf("bubble clock: start=%s elapsed=%s", start, time.Since(start))
		}
	}
	s := stack.New(stack.Options{
		NetworkProtocols:   []stack.NetworkProtocolFactory{ipv4.NewProtocol},
		TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol},
		Clock:              tcpip.NewStdClock(), SecureRNG: zeroReader{},
	})
	link := channel.New(8, 1500, "")
	defer link.Close()
	defer s.Destroy()
	if err := s.CreateNIC(1, link); err != nil {
		t.Fatal(err)
	}
	local := tcpip.AddrFrom4([4]byte{10, 0, 0, 1})
	remote := tcpip.AddrFrom4([4]byte{10, 0, 0, 2})
	addr := tcpip.ProtocolAddress{Protocol: ipv4.ProtocolNumber, AddressWithPrefix: local.WithPrefix()}
	if err := s.AddProtocolAddress(1, addr, stack.AddressProperties{}); err != nil {
		t.Fatal(err)
	}
	s.SetRouteTable([]tcpip.Route{{Destination: header.IPv4EmptySubnet, NIC: 1}})
	var queue waiter.Queue
	ep, err := s.NewEndpoint(tcp.ProtocolNumber, ipv4.ProtocolNumber, &queue)
	if err != nil {
		t.Fatal(err)
	}
	defer ep.Close()
	if err := ep.Bind(tcpip.FullAddress{Addr: local, Port: 40000}); err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	if err := ep.Connect(tcpip.FullAddress{Addr: remote, Port: 8080}); err != nil {
		if _, ok := err.(*tcpip.ErrConnectStarted); !ok {
			t.Fatal(err)
		}
	} else {
		t.Fatal("unexpected immediate connection")
	}
	parse := func(pkt *stack.PacketBuffer) uint32 {
		t.Helper()
		if pkt == nil {
			t.Fatal("no TCP packet available")
		}
		defer pkt.DecRef()
		view := pkt.ToView()
		defer view.Release()
		ip := header.IPv4(view.AsSlice())
		seg := header.TCP(ip.Payload())
		if ip.SourceAddress() != local || ip.DestinationAddress() != remote || ip.TransportProtocol() != tcp.ProtocolNumber ||
			seg.SourcePort() != 40000 || seg.DestinationPort() != 8080 || seg.Flags() != header.TCPFlagSyn || len(seg.Payload()) != 0 {
			t.Fatalf("unexpected transmitted IPv4/TCP packet: %x", view.AsSlice())
		}
		return seg.SequenceNumber()
	}
	seq := parse(link.ReadContext(context.Background()))
	fmt.Printf("SYN_SENT mode=%s src=%s:40000 dst=%s:8080 flags=SYN seq=%d time=%s go=%s\n", mode, local, remote, seq, time.Now().UTC().Format(time.RFC3339Nano), runtime.Version())
	if mode == "paused" {
		s.Pause()
		defer s.Resume()
		fmt.Println("DISPATCHER_PAUSED")
	}
	var ret uint32
	if mode == "wall" {
		ret = parse(link.ReadContext(context.Background()))
	} else {
		fmt.Printf("BEFORE_WAIT mode=%s\n", mode)
		synctest.Wait()
		fmt.Printf("AFTER_WAIT mode=%s elapsed=%s\n", mode, time.Since(start))
		time.Sleep(tcp.InitialRTO - time.Nanosecond)
		if early := link.Read(); early != nil {
			early.DecRef()
			t.Fatal("SYN retransmitted before RTO")
		}
		time.Sleep(time.Nanosecond)
		synctest.Wait()
		ret = parse(link.Read())
	}
	elapsed := time.Since(start)
	if seq != ret || elapsed < tcp.InitialRTO || (mode != "wall" && elapsed != tcp.InitialRTO) {
		t.Fatalf("initial seq=%d retransmission=%d elapsed=%s", seq, ret, elapsed)
	}
	fmt.Printf("SYN_RETRANSMITTED mode=%s src=%s:40000 dst=%s:8080 flags=SYN seq=%d elapsed=%s time=%s\n", mode, local, remote, ret, elapsed, time.Now().UTC().Format(time.RFC3339Nano))
}
