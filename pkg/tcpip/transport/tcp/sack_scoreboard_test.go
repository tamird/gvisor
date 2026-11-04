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

package tcp

import (
	"fmt"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"

	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

// buildScoreboard creates a SACKScoreboard pre-populated with n disjoint SACK
// blocks starting at iss. Each block covers [start, start+blockSize) with a
// gap of gapSize bytes between blocks, simulating selective ACKs under packet
// loss.
func buildScoreboard(iss seqnum.Value, smss uint16, n int, blockSize, gapSize seqnum.Size) *SACKScoreboard {
	s := NewSACKScoreboard(smss, iss)
	seq := iss
	for i := 0; i < n; i++ {
		// Skip a gap (the "lost" segment), then insert a SACK block.
		seq = seq.Add(gapSize)
		s.Insert(header.SACKBlock{Start: seq, End: seq.Add(blockSize)})
		seq = seq.Add(blockSize)
	}
	return s
}

// BenchmarkIsSACKED measures allocations in the IsSACKED hot path.
// Each call traverses the btree via DescendLessOrEqual, which boxes
// header.SACKBlock into btree.Item on every callback invocation.
func BenchmarkIsSACKED(b *testing.B) {
	const (
		iss       seqnum.Value = 0
		smss      uint16       = 1460
		nBlocks                = 30
		blockSize seqnum.Size  = 1460
		gapSize   seqnum.Size  = 1460
	)
	s := buildScoreboard(iss, smss, nBlocks, blockSize, gapSize)

	// Query a range that falls in the middle of the scoreboard so the
	// btree actually has to descend into it.
	mid := iss.Add(seqnum.Size(nBlocks) * (blockSize + gapSize) / 2)
	query := header.SACKBlock{Start: mid, End: mid.Add(blockSize)}

	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		s.IsSACKED(query)
	}
}

// BenchmarkIsRangeLost measures allocations in IsRangeLost.
// This path does both DescendLessOrEqual and AscendGreaterOrEqual,
// boxing on every callback.
func BenchmarkIsRangeLost(b *testing.B) {
	const (
		iss       seqnum.Value = 0
		smss      uint16       = 1460
		nBlocks                = 30
		blockSize seqnum.Size  = 1460
		gapSize   seqnum.Size  = 1460
	)
	s := buildScoreboard(iss, smss, nBlocks, blockSize, gapSize)

	// Query a gap (unsacked region) so IsRangeLost has to scan
	// multiple SACK blocks above to determine loss.
	gapStart := iss.Add((blockSize + gapSize) * 5) // start of 6th gap
	query := header.SACKBlock{Start: gapStart, End: gapStart.Add(gapSize)}

	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		s.IsRangeLost(query)
	}
}

// BenchmarkInsert measures allocations from Insert, which performs
// AscendGreaterOrEqual + DescendLessOrEqual + ReplaceOrInsert, each
// boxing SACKBlock into btree.Item.
func BenchmarkInsert(b *testing.B) {
	const (
		iss  seqnum.Value = 0
		smss uint16       = 1460
	)
	// Pre-populate with a modest scoreboard.
	s := buildScoreboard(iss, smss, 20, 1460, 1460)

	// Insert a block that overlaps with existing ranges, triggering
	// merge logic (ascend + descend + delete + insert).
	block := header.SACKBlock{
		Start: iss.Add(1460 * 9),  // overlaps ~5th gap
		End:   iss.Add(1460 * 12), // spans into existing block
	}

	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		s.Insert(block)
	}
}

// BenchmarkDelete measures allocations from Delete, which uses
// DescendLessOrEqual + Delete + ReplaceOrInsert.
func BenchmarkDelete(b *testing.B) {
	const (
		iss       seqnum.Value = 0
		smss      uint16       = 1460
		nBlocks                = 30
		blockSize seqnum.Size  = 1460
		gapSize   seqnum.Size  = 1460
	)

	// Pre-build all scoreboards to avoid StopTimer/StartTimer overhead.
	boards := make([]*SACKScoreboard, b.N)
	for i := range boards {
		boards[i] = buildScoreboard(iss, smss, nBlocks, blockSize, gapSize)
	}
	seq := iss.Add(seqnum.Size(nBlocks) * (blockSize + gapSize) / 2)

	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		boards[i].Delete(seq)
	}
}

// BenchmarkSetPipeWorkload simulates the SetPipe hot loop from the
// issue's pprof: for every unacked segment, call IsSACKED and
// IsRangeLost in sequence. This is the pattern that caused 1.4 GB of
// allocations in 30 seconds in the reporter's production workload.
func BenchmarkSetPipeWorkload(b *testing.B) {
	const (
		iss       seqnum.Value = 0
		smss      uint16       = 1460
		nBlocks                = 30
		blockSize seqnum.Size  = 1460
		gapSize   seqnum.Size  = 1460
	)
	s := buildScoreboard(iss, smss, nBlocks, blockSize, gapSize)

	total := seqnum.Size(nBlocks) * (blockSize + gapSize)

	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		// Walk through every MSS-sized segment in the range, calling
		// IsSACKED and IsRangeLost for each, as SetPipe does.
		for off := seqnum.Size(0); off < total; off += seqnum.Size(smss) {
			seg := header.SACKBlock{
				Start: iss.Add(off),
				End:   iss.Add(off + seqnum.Size(smss)),
			}
			s.IsSACKED(seg)
			s.IsRangeLost(seg)
		}
	}
}

const accountingMSS = 1460

// accountingSender creates an already-transmitted queue. No route or NIC is
// necessary: no bytes remain to transmit, and one SACK does not trigger loss
// recovery. The manual clock keeps RTO expiry out of the measured ACK path.
func accountingSender(t testing.TB, n int, una seqnum.Value) (*sender, *segment) {
	clock := faketime.NewManualClock()
	st := stack.New(stack.Options{Clock: clock})
	ep := &Endpoint{stack: st}
	ep.mu.Lock()
	ep.SACKPermitted = true
	ep.ops.SetSendBufferSize(16<<20, false)
	ep.sndQueueInfo.AutoTuneSndBufDisabled.Store(1)
	ep.sndQueueInfo.SndBufUsed = n * accountingMSS
	s := &sender{ep: ep}
	ep.snd = s
	s.SndUna = una
	s.SndWnd = seqnum.Size(n * accountingMSS)
	s.SndNxt = una.Add(s.SndWnd)
	s.RTTMeasureSeqNum = s.SndNxt
	s.FastRecovery.Last = una - 1
	s.MaxPayloadSize = accountingMSS
	s.Outstanding = n
	s.cc = s.initCongestionControl(ccReno)
	s.SndCwnd = n
	s.RTO = time.Second
	s.resendTimer.init(clock, func() { panic("unexpected timeout") })
	s.resendTimer.enable(s.RTO)
	s.writeList.set = make(map[*segment]struct{})
	ep.scoreboard = NewSACKScoreboard(accountingMSS, una)
	for i := 0; i < n; i++ {
		seg := newOutgoingSegment(stack.TransportEndpointID{}, clock, buffer.MakeWithData(make([]byte, accountingMSS)), 0)
		seg.sequenceNumber = una.Add(seqnum.Size(i * accountingMSS))
		seg.xmitCount = 1
		seg.flags = header.TCPFlagAck
		s.writeList.PushBack(seg)
	}
	ack := newOutgoingSegment(stack.TransportEndpointID{}, clock, buffer.Buffer{}, 0)
	ack.ackNumber, ack.flags, ack.window = una, header.TCPFlagAck, s.SndWnd
	t.Cleanup(func() {
		s.resendTimer.cleanup()
		ack.DecRef()
		for seg := s.writeList.Front(); seg != nil; seg = s.writeList.Front() {
			s.writeList.Remove(seg)
			seg.DecRef()
		}
		ep.mu.Unlock()
		st.Close()
		st.Wait()
	})
	return s, ack
}

func TestSACKAccountingQualification(t *testing.T) {
	const n = 16
	// The segment straddles the sequence-number wrap in the wrap cases.
	for _, base := range []string{"linear", "wrap"} {
		for _, halves := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/halves=%t", base, halves), func(t *testing.T) {
				una := seqnum.Value(1000)
				if base == "wrap" {
					una = ^seqnum.Value(0) - (n-2)*accountingMSS - accountingMSS/2
				}
				s, ack := accountingSender(t, n, una)
				start := una.Add((n - 2) * accountingMSS)
				block := header.SACKBlock{Start: start, End: start.Add(accountingMSS)}
				report := func(b header.SACKBlock) {
					ack.parsedOptions.SACKBlocks = []header.SACKBlock{b}
					ack.hasNewSACKInfo = false
					s.handleRcvdSegment(ack)
				}
				if halves {
					middle := start.Add(accountingMSS / 2)
					report(header.SACKBlock{Start: start, End: middle})
					if s.sackedOutsideRecovery != 0 || s.Outstanding != n || s.ep.scoreboard.IsSACKED(block) {
						t.Fatal("one half should not cover the segment")
					}
					report(header.SACKBlock{Start: middle, End: block.End})
				} else {
					report(block)
				}
				ranges, _ := s.ep.scoreboard.Copy()
				if len(ranges) != 1 || ranges[0] != block || s.ep.scoreboard.Sacked() != accountingMSS ||
					s.sackedOutsideRecovery != 1 || s.Outstanding != n || s.FastRecovery.Active {
					t.Fatalf("ranges=%v newly covered=%d Outstanding=%d recovery=%t", ranges, s.sackedOutsideRecovery, s.Outstanding, s.FastRecovery.Active)
				}
				// A duplicate block must not double count the covered segment.
				report(block)
				if ack.hasNewSACKInfo || s.sackedOutsideRecovery != 1 {
					t.Fatal("duplicate SACK changed accounting")
				}
				ack.parsedOptions.SACKBlocks = nil
				ack.ackNumber = block.End
				s.handleRcvdSegment(ack)
				// Leave one data segment in flight: ACKing everything would
				// unconditionally zero Outstanding and mask an accounting leak.
				if s.SndUna != block.End || s.Outstanding != 1 || s.sackedOutsideRecovery != 0 || !s.ep.scoreboard.Empty() {
					t.Fatalf("cumack: una=%d Outstanding=%d previously covered=%d scoreboard bytes=%d", s.SndUna, s.Outstanding, s.sackedOutsideRecovery, s.ep.scoreboard.Sacked())
				}
			})
		}
	}
}

func BenchmarkSACKAccountingQualification(b *testing.B) {
	for _, n := range []int{8, 128} {
		for _, pos := range []struct {
			name  string
			index int
		}{{"near", 1}, {"far", n - 2}} {
			b.Run(fmt.Sprintf("queue=%d/sack=%s", n, pos.name), func(b *testing.B) {
				const una = 1000
				s, ack := accountingSender(b, n, una)
				start := seqnum.Value(una).Add(seqnum.Size(pos.index * accountingMSS))
				block := header.SACKBlock{Start: start, End: start.Add(accountingMSS)}
				ack.parsedOptions.SACKBlocks = []header.SACKBlock{block}
				const batch = 256
				var boards [batch]*SACKScoreboard
				b.ReportAllocs()
				b.ResetTimer()
				b.StopTimer()
				for done := 0; done < b.N; {
					count := min(batch, b.N-done)
					for j := 0; j < count; j++ {
						boards[j] = NewSACKScoreboard(accountingMSS, una)
					}
					b.StartTimer()
					for j := 0; j < count; j++ {
						s.ep.scoreboard = boards[j]
						s.sackedOutsideRecovery, s.DupAckCount, s.state = 0, 0, tcpip.Open
						ack.hasNewSACKInfo = false
						s.handleRcvdSegment(ack)
					}
					b.StopTimer()
					if s.sackedOutsideRecovery != 1 || s.Outstanding != n || s.FastRecovery.Active {
						b.Fatal("unexpected flight counts")
					}
					for j := 0; j < count; j++ {
						if boards[j].Sacked() != accountingMSS || !boards[j].IsSACKED(block) {
							b.Fatal("unexpected SACK scoreboard")
						}
					}
					done += count
				}
			})
		}
	}
}
