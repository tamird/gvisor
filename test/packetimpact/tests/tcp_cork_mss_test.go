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
	"encoding/json"
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

// TestMSSSACKObservation measures the same application write with timestamps
// omitted. The receive-gap arm also makes the outgoing SACK option nonempty.
// This is a packet-size observation, not a throughput benchmark.
func TestMSSSACKObservation(t *testing.T) {
	for _, tc := range []struct {
		name string
		sack bool
		gap  bool
	}{
		{"no_sack", false, false},
		{"sack", true, false},
		{"sack_receive_gap", true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dut := testbench.NewDUT(t)
			listener, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
			defer dut.Close(t, listener)
			conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &port}, testbench.TCP{SrcPort: &port})
			defer conn.Close(t)
			options := make([]byte, 8)
			n := header.EncodeMSSOption(1460, options)
			if tc.sack {
				n += header.EncodeNOP(options[n:])
				n += header.EncodeNOP(options[n:])
				n += header.EncodeSACKPermittedOption(options[n:])
			}
			conn.ConnectWithOptions(t, options[:n])
			synAck := conn.SynAck(t)
			negotiated := header.ParseSynOptions(synAck.Options, true)
			if negotiated.TS || negotiated.SACKPermitted != tc.sack {
				t.Fatalf("unexpected SYN-ACK options: %x (%+v)", synAck.Options, negotiated)
			}
			fd, _ := dut.Accept(t, listener)
			defer dut.Close(t, fd)
			dut.SetSockOptInt(t, fd, unix.IPPROTO_TCP, unix.TCP_NODELAY, 1)
			clientSeq := uint32(*conn.LocalSeqNum(t))
			var gapOptions []byte
			if tc.gap {
				conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), SeqNum: testbench.Uint32(clientSeq + 1)}, &testbench.Payload{Bytes: []byte{'b'}})
				ack, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: &clientSeq}, time.Second)
				if err != nil {
					t.Fatalf("receive-gap ACK: %s", err)
				}
				gapOptions = ack.Options
				blocks := header.ParseTCPOptions(ack.Options).SACKBlocks
				if len(blocks) != 1 || uint32(blocks[0].Start) != clientSeq+1 || uint32(blocks[0].End) != clientSeq+2 {
					t.Fatalf("receive-gap SACK = %+v", blocks)
				}
			}
			before := dut.GetSockOptTCPInfo(t, fd)
			payload := bytes.Repeat([]byte{0x5a}, 1460)
			if got := dut.Send(t, fd, payload, 0); got != int32(len(payload)) {
				t.Fatalf("single Send = %d, want %d", got, len(payload))
			}
			var packets []map[string]any
			for received := 0; received < len(payload); {
				seq := uint32(*conn.RemoteSeqNum(t))
				frame, err := conn.ExpectNextData(t, &testbench.TCP{SeqNum: &seq, AckNum: &clientSeq}, nil, time.Second)
				if err != nil {
					t.Fatalf("data after %d bytes: %s", received, err)
				}
				if len(frame) != 4 {
					t.Fatalf("want Ethernet/IPv4/TCP/payload, got %s", frame)
				}
				tcp := frame[2].(*testbench.TCP)
				data := frame[3].(*testbench.Payload).Bytes
				if len(data) == 0 || len(data) > len(payload)-received || !bytes.Equal(data, payload[received:received+len(data)]) {
					t.Fatalf("unexpected payload at %d: %x", received, data)
				}
				parsed := header.ParseTCPOptions(tcp.Options)
				if parsed.TS || (!tc.sack && len(parsed.SACKBlocks) > 0) {
					t.Fatalf("unexpected data options: %x", tcp.Options)
				}
				packets = append(packets, map[string]any{"payload": len(data), "options": tcp.Options, "header_bytes": *tcp.DataOffset, "seq": *tcp.SeqNum, "sack_blocks": parsed.SACKBlocks})
				received += len(data)
				conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), SeqNum: &clientSeq})
			}
			if tc.gap {
				conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), SeqNum: &clientSeq}, &testbench.Payload{Bytes: []byte{'a'}})
				if got := dut.Recv(t, fd, 2, unix.MSG_WAITALL); !bytes.Equal(got, []byte("ab")) {
					t.Fatalf("filled receive gap: %q", got)
				}
			}
			var uts unix.Utsname
			if err := unix.Uname(&uts); err != nil {
				t.Fatal(err)
			}
			observation := map[string]any{"case": tc.name, "syn_options": options[:n], "syn_ack_options": synAck.Options, "negotiated": negotiated, "before": before, "after": dut.GetSockOptTCPInfo(t, fd), "packets": packets, "send_bytes": len(payload), "tcp_nodelay": 1, "gap_ack_options": gapOptions, "worker_kernel": unix.ByteSliceToString(uts.Release[:])}
			encoded, err := json.Marshal(observation)
			if err != nil {
				t.Fatal(err)
			}
			t.Logf("MSSOBS %s", encoded)
		})
	}
}
