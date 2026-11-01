// Copyright 2020 The gVisor Authors.
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

package tcp_cork_mss_test

import (
	"bytes"
	"flag"
	"fmt"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/test/packetimpact/testbench"
)

func init() {
	testbench.Initialize(flag.CommandLine)
}

// TestTCPCorkMSS tests for segment coalesce and split as per MSS.
func TestTCPCorkMSS(t *testing.T) {
	dut := testbench.NewDUT(t)
	listenFD, remotePort := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
	defer dut.Close(t, listenFD)
	conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &remotePort}, testbench.TCP{SrcPort: &remotePort})
	defer conn.Close(t)

	const mss = uint32(header.TCPDefaultMSS)
	options := make([]byte, header.TCPOptionMSSLength)
	header.EncodeMSSOption(mss, options)
	conn.ConnectWithOptions(t, options)

	acceptFD, _ := dut.Accept(t, listenFD)
	defer dut.Close(t, acceptFD)

	dut.SetSockOptInt(t, acceptFD, unix.IPPROTO_TCP, unix.TCP_CORK, 1)

	// Let the dut application send 2 small segments to be held up and coalesced
	// until the application sends a larger segment to fill up to > MSS.
	sampleData := []byte("Sample Data")
	dut.Send(t, acceptFD, sampleData, 0)
	dut.Send(t, acceptFD, sampleData, 0)

	expectedData := sampleData
	expectedData = append(expectedData, sampleData...)
	largeData := make([]byte, mss+1)
	expectedData = append(expectedData, largeData...)
	dut.Send(t, acceptFD, largeData, 0)

	// Expect the segments to be coalesced and sent and capped to MSS.
	expectedPayload := testbench.Payload{Bytes: expectedData[:mss]}
	if _, err := conn.ExpectData(t, &testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck)}, &expectedPayload, time.Second); err != nil {
		t.Fatalf("expected payload was not received: %s", err)
	}
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck)})
	// Expect the coalesced segment to be split and transmitted.
	expectedPayload = testbench.Payload{Bytes: expectedData[mss:]}
	if _, err := conn.ExpectData(t, &testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck | header.TCPFlagPsh)}, &expectedPayload, time.Second); err != nil {
		t.Fatalf("expected payload was not received: %s", err)
	}

	// Check for segments to *not* be held up because of TCP_CORK when
	// the current send window is less than MSS.
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), WindowSize: testbench.Uint16(uint16(2 * len(sampleData)))})
	dut.Send(t, acceptFD, sampleData, 0)
	dut.Send(t, acceptFD, sampleData, 0)
	expectedPayload = testbench.Payload{Bytes: append(sampleData, sampleData...)}
	if _, err := conn.ExpectData(t, &testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck | header.TCPFlagPsh)}, &expectedPayload, time.Second); err != nil {
		t.Fatalf("expected payload was not received: %s", err)
	}
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck)})
}

// TestQueuedWriteBoundary observes the first segment at the tail of a queued
// 32 KiB write. Accepting each application write behind a closed window keeps
// scheduling of the application separate from the sender's packetization.
func TestQueuedWriteBoundary(t *testing.T) {
	const mss, first, tail = 1420, 32 << 10, 108
	for _, tc := range []struct {
		name string
		next int
	}{
		{"Large", first},
		{"Fits", mss - tail},
		{"Straddles", mss - tail + 1},
		{"Fin", 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dut := testbench.NewDUT(t)
			listener, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
			defer dut.Close(t, listener)
			conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &port, WindowSize: testbench.Uint16(0)}, testbench.TCP{SrcPort: &port})
			defer conn.Close(t)
			opt := make([]byte, header.TCPOptionMSSLength)
			header.EncodeMSSOption(mss, opt)
			conn.ConnectWithOptions(t, opt)
			fd, _ := dut.Accept(t, listener)
			defer dut.Close(t, fd)
			dut.SetSockOptInt(t, fd, unix.SOL_SOCKET, unix.SO_SNDBUF, 256<<10)
			dut.SetSockOptInt(t, fd, unix.IPPROTO_TCP, unix.TCP_NODELAY, 1)
			if n, c := dut.GetSockOptInt(t, fd, unix.IPPROTO_TCP, unix.TCP_NODELAY), dut.GetSockOptInt(t, fd, unix.IPPROTO_TCP, unix.TCP_CORK); n != 1 || c != 0 {
				t.Fatalf("NODELAY=%d CORK=%d, want 1 and 0", n, c)
			}

			a, b := bytes.Repeat([]byte{'a'}, first), bytes.Repeat([]byte{'b'}, tc.next)
			if got := dut.Send(t, fd, a, 0); int(got) != len(a) {
				t.Fatalf("first write accepted %d of %d bytes", got, len(a))
			}
			if tc.next != 0 {
				if got := dut.Send(t, fd, b, 0); int(got) != len(b) {
					t.Fatalf("second write accepted %d of %d bytes", got, len(b))
				}
			} else {
				dut.Shutdown(t, fd, unix.SHUT_WR)
			}
			want := append(a, b...)
			ack := func() {
				conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), WindowSize: testbench.Uint16(mss)})
			}
			ack()
			seen, prefixPackets, boundary := 0, 0, 0
			for {
				// Match the next in-sequence packet even when its only content is FIN.
				layers, err := conn.ExpectData(t, &testbench.TCP{}, nil, 2*time.Second)
				if err != nil {
					t.Fatalf("at byte %d of %d: %v", seen, len(want), err)
				}
				tcp := layers[2].(*testbench.TCP)
				fin := *tcp.Flags&header.TCPFlagFin != 0
				var data []byte
				if len(layers) > 3 {
					data = layers[3].(*testbench.Payload).Bytes
				}
				if len(data) > mss || seen+len(data) > len(want) || !bytes.Equal(data, want[seen:seen+len(data)]) {
					t.Fatalf("unexpected payload of %d bytes at offset %d", len(data), seen)
				}
				if len(data) != 0 {
					switch {
					case seen < first-tail:
						if len(data) != mss {
							t.Fatalf("prefix packet at %d had %d bytes, want %d", seen, len(data), mss)
						}
						prefixPackets++
					case seen == first-tail:
						boundary = len(data)
						fmt.Printf("QUEUED_BOUNDARY name=%s first=%d second=%d prefix=%d prefix_packets=%d window=%d first_payload=%d\n",
							tc.name, first, tc.next, seen, prefixPackets, mss, boundary)
					}
					seen += len(data)
					if tc.next != 0 && seen == len(want) {
						dut.Shutdown(t, fd, unix.SHUT_WR)
					}
				}
				if fin {
					if seen != len(want) || prefixPackets != 23 || boundary == 0 {
						t.Fatalf("FIN at %d, want %d, prefix=%d boundary=%d", seen, len(want), prefixPackets, boundary)
					}
					ack()
					fmt.Printf("QUEUED_FIN name=%s bytes=%d\n", tc.name, seen)
					break
				}
				if len(data) != 0 {
					ack()
				}
			}
		})
	}
}
