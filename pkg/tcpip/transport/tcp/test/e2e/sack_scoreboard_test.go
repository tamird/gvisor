// Copyright 2018 The gVisor Authors.
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

package sack_scoreboard_test

import (
	"os"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/refs"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
)

const smss = 1500

var scoreboardSink *tcp.SACKScoreboard

func TestSACKScoreboardAllocations(t *testing.T) {
	t.Cleanup(func() { scoreboardSink = nil })
	if got := testing.AllocsPerRun(100, func() {
		scoreboardSink = tcp.NewSACKScoreboard(smss, 100)
	}); got > 1 {
		t.Errorf("NewSACKScoreboard allocations = %g, want at most 1", got)
	}
	s := tcp.NewSACKScoreboard(smss, 100)
	if got := testing.AllocsPerRun(100, s.Reset); got != 0 {
		t.Errorf("Reset allocations = %g, want 0", got)
	}
}

func initScoreboard(blocks []header.SACKBlock, iss seqnum.Value) *tcp.SACKScoreboard {
	s := tcp.NewSACKScoreboard(smss, iss)
	for _, blk := range blocks {
		s.Insert(blk)
	}
	return s
}

func TestSACKScoreboardIsSACKED(t *testing.T) {
	type blockTest struct {
		block  header.SACKBlock
		sacked bool
	}
	testCases := []struct {
		comment          string
		scoreboardBlocks []header.SACKBlock
		blockTests       []blockTest
		iss              seqnum.Value
	}{
		{
			"Test holes and unsacked SACK blocks in SACKed ranges and insertion of overlapping SACK blocks",
			[]header.SACKBlock{{Start: 10, End: 20}, {Start: 10, End: 30}, {Start: 30, End: 40}, {Start: 41, End: 50}, {Start: 5, End: 10}, {Start: 1, End: 50}, {Start: 111, End: 120}, {Start: 101, End: 110}, {Start: 52, End: 120}},
			[]blockTest{
				{header.SACKBlock{Start: 15, End: 21}, true},
				{header.SACKBlock{Start: 200, End: 201}, false},
				{header.SACKBlock{Start: 50, End: 51}, false},
				{header.SACKBlock{Start: 53, End: 120}, true},
			},
			0,
		},
		{
			"Test disjoint SACKBlocks",
			[]header.SACKBlock{{Start: 2288624809, End: 2288810057}, {Start: 2288811477, End: 2288838565}},
			[]blockTest{
				{header.SACKBlock{Start: 2288624809, End: 2288810057}, true},
				{header.SACKBlock{Start: 2288811477, End: 2288838565}, true},
				{header.SACKBlock{Start: 2288810057, End: 2288811477}, false},
			},
			2288624809,
		},
		{
			"Test sequence number wrap around",
			[]header.SACKBlock{{Start: 4294254144, End: 225652}, {Start: 5340409, End: 5350509}},
			[]blockTest{
				{header.SACKBlock{Start: 4294254144, End: 4294254145}, true},
				{header.SACKBlock{Start: 4294254143, End: 4294254144}, false},
				{header.SACKBlock{Start: 4294254144, End: 1}, true},
				{header.SACKBlock{Start: 225652, End: 5350509}, false},
				{header.SACKBlock{Start: 5340409, End: 5350509}, true},
				{header.SACKBlock{Start: 5350509, End: 5350609}, false},
			},
			4294254144,
		},
		{
			"Test disjoint SACKBlocks out of order",
			[]header.SACKBlock{{Start: 827450276, End: 827454536}, {Start: 827426028, End: 827428868}},
			[]blockTest{
				{header.SACKBlock{Start: 827426028, End: 827428867}, true},
				{header.SACKBlock{Start: 827450168, End: 827450275}, false},
			},
			827426000,
		},
	}
	for _, tc := range testCases {
		sb := initScoreboard(tc.scoreboardBlocks, tc.iss)
		for _, blkTest := range tc.blockTests {
			if want, got := blkTest.sacked, sb.IsSACKED(blkTest.block); got != want {
				t.Errorf("%s: s.IsSACKED(%v) = %v, want %v", tc.comment, blkTest.block, got, want)
			}
		}
	}
}

func TestSACKScoreboardIsRangeLost(t *testing.T) {
	const largeMSS = 1 << 15
	for _, test := range []struct {
		name        string
		mss         uint16
		sackedBytes seqnum.Size
		lost        bool
	}{
		{name: "below_byte_threshold", mss: smss, sackedBytes: 2*smss - 1},
		{name: "at_byte_threshold", mss: smss, sackedBytes: 2 * smss},
		{name: "above_byte_threshold", mss: smss, sackedBytes: 2*smss + 1, lost: true},
		{name: "large_mss_below_threshold", mss: largeMSS, sackedBytes: 2*largeMSS - 1},
		{name: "large_mss_at_threshold", mss: largeMSS, sackedBytes: 2 * largeMSS},
		{name: "large_mss_above_threshold", mss: largeMSS, sackedBytes: 2*largeMSS + 1, lost: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			s := tcp.NewSACKScoreboard(test.mss, 0)
			start := seqnum.Value(test.mss)
			s.Insert(header.SACKBlock{Start: start, End: start.Add(test.sackedBytes)})
			block := header.SACKBlock{Start: 0, End: 1}
			if got, want := s.IsRangeLost(block), test.lost; got != want {
				t.Errorf("IsRangeLost(%v) with %d bytes SACKed = %t, want %t", block, test.sackedBytes, got, want)
			}
		})
	}

	s := tcp.NewSACKScoreboard(10, 0)
	s.Insert(header.SACKBlock{Start: 1, End: 25})
	s.Insert(header.SACKBlock{Start: 25, End: 50})
	s.Insert(header.SACKBlock{Start: 51, End: 100})
	s.Insert(header.SACKBlock{Start: 111, End: 120})
	s.Insert(header.SACKBlock{Start: 101, End: 110})
	s.Insert(header.SACKBlock{Start: 121, End: 141})
	s.Insert(header.SACKBlock{Start: 145, End: 146})
	s.Insert(header.SACKBlock{Start: 147, End: 148})
	s.Insert(header.SACKBlock{Start: 149, End: 150})
	s.Insert(header.SACKBlock{Start: 153, End: 154})
	s.Insert(header.SACKBlock{Start: 155, End: 156})
	testCases := []struct {
		block header.SACKBlock
		lost  bool
	}{
		// Block not covered by SACK block and has more than
		// nDupAckThreshold discontiguous SACK blocks after it as well
		// as (nDupAckThreshold -1) * 10 (smss) bytes that have been
		// SACKED above the sequence number covered by this block.
		{block: header.SACKBlock{Start: 0, End: 1}, lost: true},

		// These blocks have all been SACKed and should not be
		// considered lost.
		{block: header.SACKBlock{Start: 1, End: 2}, lost: false},
		{block: header.SACKBlock{Start: 25, End: 26}, lost: false},
		{block: header.SACKBlock{Start: 1, End: 45}, lost: false},

		// Same as the first case above.
		{block: header.SACKBlock{Start: 50, End: 51}, lost: true},

		// This block has been SACKed and should not be considered lost.
		{block: header.SACKBlock{Start: 119, End: 120}, lost: false},

		// This one should return true because there are >
		// (nDupAckThreshold - 1) * 10 (smss) bytes that have been
		// sacked above this sequence number.
		{block: header.SACKBlock{Start: 120, End: 121}, lost: true},

		// This block has been SACKed and should not be considered lost.
		{block: header.SACKBlock{Start: 125, End: 126}, lost: false},

		// This block has not been SACKed and there are nDupAckThreshold
		// number of SACKed blocks after it.
		{block: header.SACKBlock{Start: 141, End: 145}, lost: true},

		// This block has not been SACKed and there are less than
		// nDupAckThreshold SACKed sequences after it.
		{block: header.SACKBlock{Start: 151, End: 152}, lost: false},
	}
	for _, tc := range testCases {
		if want, got := tc.lost, s.IsRangeLost(tc.block); got != want {
			t.Errorf("s.IsRangeLost(%v) = %v, want %v", tc.block, got, want)
		}
	}
}

func TestSACKScoreboardIsLost(t *testing.T) {
	s := tcp.NewSACKScoreboard(10, 0)
	s.Insert(header.SACKBlock{Start: 1, End: 25})
	s.Insert(header.SACKBlock{Start: 25, End: 50})
	s.Insert(header.SACKBlock{Start: 51, End: 100})
	s.Insert(header.SACKBlock{Start: 111, End: 120})
	s.Insert(header.SACKBlock{Start: 101, End: 110})
	s.Insert(header.SACKBlock{Start: 121, End: 141})
	s.Insert(header.SACKBlock{Start: 121, End: 141})
	s.Insert(header.SACKBlock{Start: 145, End: 146})
	s.Insert(header.SACKBlock{Start: 147, End: 148})
	s.Insert(header.SACKBlock{Start: 149, End: 150})
	s.Insert(header.SACKBlock{Start: 153, End: 154})
	s.Insert(header.SACKBlock{Start: 155, End: 156})
	testCases := []struct {
		seq  seqnum.Value
		lost bool
	}{
		// Sequence number not covered by SACK block and has more than
		// nDupAckThreshold discontiguous SACK blocks after it as well
		// as (nDupAckThreshold -1) * 10 (smss) bytes that have been
		// SACKED above the sequence number.
		{seq: 0, lost: true},

		// These sequence numbers have all been SACKed and should not be
		// considered lost.
		{seq: 1, lost: false},
		{seq: 25, lost: false},
		{seq: 45, lost: false},

		// Same as first case above.
		{seq: 50, lost: true},

		// This block has been SACKed and should not be considered lost.
		{seq: 119, lost: false},

		// This one should return true because there are >
		// (nDupAckThreshold - 1) * 10 (smss) bytes that have been
		// sacked above this sequence number.
		{seq: 120, lost: true},

		// This sequence number has been SACKed and should not be
		// considered lost.
		{seq: 125, lost: false},

		// This sequence number has not been SACKed and there are
		// nDupAckThreshold number of SACKed blocks after it.
		{seq: 141, lost: true},

		// This sequence number has not been SACKed and there are less
		// than nDupAckThreshold SACKed sequences after it.
		{seq: 151, lost: false},
	}
	for _, tc := range testCases {
		if want, got := tc.lost, s.IsLost(tc.seq); got != want {
			t.Errorf("s.IsLost(%v) = %v, want %v", tc.seq, got, want)
		}
	}
}

func TestSACKScoreboardDelete(t *testing.T) {
	blocks := []header.SACKBlock{{Start: 4294254144, End: 225652}, {Start: 5340409, End: 5350509}}
	s := initScoreboard(blocks, 4294254143)
	s.Delete(5340408)
	if s.Empty() {
		t.Fatalf("s.Empty() = true, want false")
	}
	if got, want := s.Sacked(), blocks[1].Start.Size(blocks[1].End); got != want {
		t.Fatalf("incorrect sacked bytes in scoreboard got: %v, want: %v", got, want)
	}
	s.Delete(5340410)
	if s.Empty() {
		t.Fatal("s.Empty() = true, want false")
	}
	newSB := header.SACKBlock{Start: 5340410, End: 5350509}
	if !s.IsSACKED(newSB) {
		t.Fatalf("s.IsSACKED(%v) = false, want true, scoreboard: %v", newSB, s)
	}
	s.Delete(5350509)
	lastOctet := header.SACKBlock{Start: 5350508, End: 5350509}
	if s.IsSACKED(lastOctet) {
		t.Fatalf("s.IsSACKED(%v) = false, want true", lastOctet)
	}

	s.Delete(5350510)
	if !s.Empty() {
		t.Fatal("s.Empty() = false, want true")
	}
	if got, want := s.Sacked(), seqnum.Size(0); got != want {
		t.Fatalf("incorrect sacked bytes in scoreboard got: %v, want: %v", got, want)
	}
}

func TestMain(m *testing.M) {
	refs.SetLeakMode(refs.LeaksPanic)
	code := m.Run()
	// Allow TCP async work to complete to avoid false reports of leaks.
	// TODO(gvisor.dev/issue/5940): Use fake clock in tests.
	time.Sleep(1 * time.Second)
	refs.DoLeakCheck()
	os.Exit(code)
}
