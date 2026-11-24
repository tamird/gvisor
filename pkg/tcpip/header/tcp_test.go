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

package header_test

import (
	"reflect"
	"slices"
	"testing"

	"gvisor.dev/gvisor/pkg/tcpip/header"
)

func TestEncodeSACKBlocks(t *testing.T) {
	testCases := []struct {
		sackBlocks []header.SACKBlock
		want       []header.SACKBlock
		bufSize    int
	}{
		{
			[]header.SACKBlock{{Start: 10, End: 20}, {Start: 22, End: 30}, {Start: 32, End: 40}, {Start: 42, End: 50}, {Start: 52, End: 60}, {Start: 62, End: 70}},
			[]header.SACKBlock{{Start: 10, End: 20}, {Start: 22, End: 30}, {Start: 32, End: 40}, {Start: 42, End: 50}},
			40,
		},
		{
			[]header.SACKBlock{{Start: 10, End: 20}, {Start: 22, End: 30}, {Start: 32, End: 40}, {Start: 42, End: 50}, {Start: 52, End: 60}, {Start: 62, End: 70}},
			[]header.SACKBlock{{Start: 10, End: 20}, {Start: 22, End: 30}, {Start: 32, End: 40}},
			30,
		},
		{
			[]header.SACKBlock{{Start: 10, End: 20}, {Start: 22, End: 30}, {Start: 32, End: 40}, {Start: 42, End: 50}, {Start: 52, End: 60}, {Start: 62, End: 70}},
			[]header.SACKBlock{{Start: 10, End: 20}, {Start: 22, End: 30}},
			20,
		},
		{
			[]header.SACKBlock{{Start: 10, End: 20}, {Start: 22, End: 30}, {Start: 32, End: 40}, {Start: 42, End: 50}, {Start: 52, End: 60}, {Start: 62, End: 70}},
			[]header.SACKBlock{{Start: 10, End: 20}},
			10,
		},
		{
			[]header.SACKBlock{{Start: 10, End: 20}, {Start: 22, End: 30}, {Start: 32, End: 40}, {Start: 42, End: 50}, {Start: 52, End: 60}, {Start: 62, End: 70}},
			nil,
			8,
		},
		{
			[]header.SACKBlock{{Start: 10, End: 20}, {Start: 22, End: 30}, {Start: 32, End: 40}, {Start: 42, End: 50}, {Start: 52, End: 60}, {Start: 62, End: 70}},
			[]header.SACKBlock{{Start: 10, End: 20}, {Start: 22, End: 30}, {Start: 32, End: 40}, {Start: 42, End: 50}},
			60,
		},
	}
	for _, tc := range testCases {
		b := make([]byte, tc.bufSize)
		t.Logf("testing: %v", tc)
		header.EncodeSACKBlocks(tc.sackBlocks, b)
		opts := header.ParseTCPOptions(b)
		if got, want := opts.SACKBlocks, tc.want; !slices.Equal(got, want) {
			t.Errorf("header.EncodeSACKBlocks(%v, %v), encoded blocks got: %v, want: %v", tc.sackBlocks, b, got, want)
		}
	}
}

func TestTCPParseOptions(t *testing.T) {
	type tsOption struct {
		tsVal uint32
		tsEcr uint32
	}

	generateOptions := func(tsOpt *tsOption, sackBlocks []header.SACKBlock) []byte {
		l := 0
		if tsOpt != nil {
			l += 10
		}
		if len(sackBlocks) != 0 {
			l += len(sackBlocks)*8 + 2
		}
		b := make([]byte, l)
		offset := 0
		if tsOpt != nil {
			offset = header.EncodeTSOption(tsOpt.tsVal, tsOpt.tsEcr, b)
		}
		header.EncodeSACKBlocks(sackBlocks, b[offset:])
		return b
	}

	testCases := []struct {
		b    []byte
		want header.TCPOptions
	}{
		// Trivial cases.
		{nil, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
		{[]byte{header.TCPOptionNOP}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
		{[]byte{header.TCPOptionNOP, header.TCPOptionNOP}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
		{[]byte{header.TCPOptionEOL}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
		{[]byte{header.TCPOptionNOP, header.TCPOptionEOL, header.TCPOptionTS, 10, 1, 1}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},

		// Test timestamp parsing.
		{[]byte{header.TCPOptionNOP, header.TCPOptionTS, 10, 0, 0, 0, 1, 0, 0, 0, 1}, header.TCPOptions{TS: true, TSVal: 1, TSEcr: 1, SACKBlocks: nil}},
		{[]byte{header.TCPOptionTS, 10, 0, 0, 0, 1, 0, 0, 0, 1}, header.TCPOptions{TS: true, TSVal: 1, TSEcr: 1, SACKBlocks: nil}},

		// Test malformed timestamp option.
		{[]byte{header.TCPOptionTS, 8, 1, 1}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
		{[]byte{header.TCPOptionNOP, header.TCPOptionTS, 8, 1, 1}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
		{[]byte{header.TCPOptionNOP, header.TCPOptionTS, 8, 0, 0, 0, 1, 0, 0, 0, 1}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},

		// Test SACKBlock parsing.
		{[]byte{header.TCPOptionSACK, 10, 0, 0, 0, 1, 0, 0, 0, 10}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: []header.SACKBlock{{Start: 1, End: 10}}}},
		{[]byte{header.TCPOptionSACK, 18, 0, 0, 0, 1, 0, 0, 0, 10, 0, 0, 0, 11, 0, 0, 0, 12}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: []header.SACKBlock{{Start: 1, End: 10}, {Start: 11, End: 12}}}},

		// Test malformed SACK option.
		{[]byte{header.TCPOptionSACK, 0}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
		{[]byte{header.TCPOptionSACK, 8, 0, 0, 0, 1, 0, 0, 0, 10}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
		{[]byte{header.TCPOptionSACK, 11, 0, 0, 0, 1, 0, 0, 0, 10, 0, 0, 0, 11, 0, 0, 0, 12}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
		{[]byte{header.TCPOptionSACK, 17, 0, 0, 0, 1, 0, 0, 0, 10, 0, 0, 0, 11, 0, 0, 0, 12}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
		{[]byte{header.TCPOptionSACK}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
		{[]byte{header.TCPOptionSACK, 10}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
		{[]byte{header.TCPOptionSACK, 10, 0, 0, 0, 1, 0, 0, 0}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},

		// Test Timestamp + SACK block parsing.
		{generateOptions(&tsOption{1, 1}, []header.SACKBlock{{Start: 1, End: 10}, {Start: 11, End: 12}}), header.TCPOptions{TS: true, TSVal: 1, TSEcr: 1, SACKBlocks: []header.SACKBlock{{Start: 1, End: 10}, {Start: 11, End: 12}}}},
		{generateOptions(&tsOption{1, 2}, []header.SACKBlock{{Start: 1, End: 10}, {Start: 11, End: 12}}), header.TCPOptions{TS: true, TSVal: 1, TSEcr: 2, SACKBlocks: []header.SACKBlock{{Start: 1, End: 10}, {Start: 11, End: 12}}}},
		{generateOptions(&tsOption{1, 3}, []header.SACKBlock{{Start: 1, End: 10}, {Start: 11, End: 12}, {Start: 13, End: 14}, {Start: 14, End: 15}, {Start: 15, End: 16}}), header.TCPOptions{TS: true, TSVal: 1, TSEcr: 3, SACKBlocks: []header.SACKBlock{{Start: 1, End: 10}, {Start: 11, End: 12}, {Start: 13, End: 14}, {Start: 14, End: 15}}}},

		// Test valid timestamp + malformed SACK block parsing.
		{[]byte{header.TCPOptionTS, 10, 0, 0, 0, 1, 0, 0, 0, 1, header.TCPOptionSACK}, header.TCPOptions{TS: true, TSVal: 1, TSEcr: 1, SACKBlocks: nil}},
		{[]byte{header.TCPOptionTS, 10, 0, 0, 0, 1, 0, 0, 0, 1, header.TCPOptionSACK, 10}, header.TCPOptions{TS: true, TSVal: 1, TSEcr: 1, SACKBlocks: nil}},
		{[]byte{header.TCPOptionTS, 10, 0, 0, 0, 1, 0, 0, 0, 1, header.TCPOptionSACK, 10, 0, 0, 0}, header.TCPOptions{TS: true, TSVal: 1, TSEcr: 1, SACKBlocks: nil}},
		{[]byte{header.TCPOptionTS, 10, 0, 0, 0, 1, 0, 0, 0, 1, header.TCPOptionSACK, 11, 0, 0, 0, 1, 0, 0, 0, 1}, header.TCPOptions{TS: true, TSVal: 1, TSEcr: 1, SACKBlocks: nil}},
		{[]byte{header.TCPOptionSACK, header.TCPOptionTS, 10, 0, 0, 0, 1, 0, 0, 0, 1}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
		{[]byte{header.TCPOptionSACK, 10, header.TCPOptionTS, 10, 0, 0, 0, 1, 0, 0, 0, 1}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: []header.SACKBlock{{Start: 134873088, End: 65536}}}},
		{[]byte{header.TCPOptionSACK, 10, 0, 0, 0, header.TCPOptionTS, 10, 0, 0, 0, 1, 0, 0, 0, 1}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: []header.SACKBlock{{Start: 8, End: 167772160}}}},
		{[]byte{header.TCPOptionSACK, 11, 0, 0, 0, 1, 0, 0, 0, 1, header.TCPOptionTS, 10, 0, 0, 0, 1, 0, 0, 0, 1}, header.TCPOptions{TS: false, TSVal: 0, TSEcr: 0, SACKBlocks: nil}},
	}
	for _, tc := range testCases {
		if got, want := header.ParseTCPOptions(tc.b), tc.want; !reflect.DeepEqual(got, want) {
			t.Errorf("ParseTCPOptions(%v) = %v, want: %v", tc.b, got, tc.want)
		}
	}
}

func TestTCPFlags(t *testing.T) {
	for _, tt := range []struct {
		want  string
		flags header.TCPFlags
	}{
		{flags: header.TCPFlagFin, want: "F       "},
		{flags: header.TCPFlagSyn, want: " S      "},
		{flags: header.TCPFlagRst, want: "  R     "},
		{flags: header.TCPFlagPsh, want: "   P    "},
		{flags: header.TCPFlagAck, want: "    A   "},
		{flags: header.TCPFlagUrg, want: "     U  "},
		{flags: header.TCPFlagEce, want: "      E "},
		{flags: header.TCPFlagCwr, want: "       C"},
		{flags: header.TCPFlagSyn | header.TCPFlagAck, want: " S  A   "},
		{flags: header.TCPFlagFin | header.TCPFlagAck, want: "F   A   "},
	} {
		if got := tt.flags.String(); got != tt.want {
			t.Errorf("got TCPFlags(%#b).String() = %s, want = %s", tt.flags, got, tt.want)
		}
	}
}
