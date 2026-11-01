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

package tcp_duplicate_syn_test

import (
	"flag"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/test/packetimpact/testbench"
)

func init() {
	testbench.Initialize(flag.CommandLine)
}

func TestNoDuplicateSYN(t *testing.T) {
	observeHandshake(t, false)
}

func TestPassiveDuplicateSYN(t *testing.T) {
	observeHandshake(t, true)
}

func observeHandshake(t *testing.T, duplicate bool) {
	t.Helper()
	dut := testbench.NewDUT(t)
	listener, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 8)
	defer dut.Close(t, listener)
	conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &port}, testbench.TCP{SrcPort: &port})
	defer conn.Close(t)
	syn := conn.CreateFrame(t, testbench.Layers{&testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn)}})
	start := time.Now()
	conn.SendFrame(t, syn)
	synACK := header.TCPFlagSyn | header.TCPFlagAck
	first, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(synACK)}, 300*time.Millisecond)
	if err != nil {
		t.Fatalf("initial SYN-ACK: %s", err)
	}
	if duplicate {
		conn.SendFrameStateless(t, syn)
	}

	// Keep all frames, including a bare ACK which would be filtered out
	// by Expect. End the observation well before the initial 1s RTO.
	frames, _ := conn.ListenForFrame(t, testbench.Layers{nil, nil, &testbench.TCP{Flags: testbench.TCPFlags(synACK), SeqNum: first.SeqNum}}, 200*time.Millisecond)
	var responses []string
	for _, frame := range frames {
		for _, layer := range frame {
			p, ok := layer.(*testbench.TCP)
			if !ok || *p.SrcPort != *first.SrcPort || *p.DstPort != *first.DstPort {
				continue
			}
			switch {
			case *p.Flags == synACK && *p.SeqNum == *first.SeqNum && *p.AckNum == *first.AckNum:
				responses = append(responses, "SYN-ACK")
			case *p.Flags == header.TCPFlagAck && *p.SeqNum == *first.SeqNum+1 && *p.AckNum == *first.AckNum:
				responses = append(responses, "ACK")
			default:
				t.Fatalf("unexpected response on this connection: %+v", p)
			}
		}
	}
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck)})
	dut.PollOne(t, listener, unix.POLLIN, time.Second)
	fd, _ := dut.Accept(t, listener)
	defer dut.Close(t, fd)
	elapsed := time.Since(start)
	if elapsed >= 800*time.Millisecond {
		t.Fatalf("handshake took %s; cannot exclude a timer retransmission", elapsed)
	}
	child := dut.GetSockOptTCPInfo(t, fd).TotalRetrans
	parent := dut.GetSockOptTCPInfo(t, listener).TotalRetrans
	t.Logf("DUPSYN duplicate=%t responses=%v child=%d listener=%d handshake_us=%d", duplicate, responses, child, parent, elapsed.Microseconds())
	if duplicate {
		if len(responses) != 1 || responses[0] != "SYN-ACK" {
			t.Errorf("repeated SYN got %v, want one SYN-ACK", responses)
		}
	} else if len(responses) != 0 || child != 0 {
		t.Errorf("no retransmission requested; got %v, total %d", responses, child)
	}
	if parent != 0 {
		t.Errorf("listener total retransmissions = %d, want 0", parent)
	}
}
