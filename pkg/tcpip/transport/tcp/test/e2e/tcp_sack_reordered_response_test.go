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

package tcp_sack_test

import (
	"fmt"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp/testing/context"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

func TestEifelResponseAfterReorderedACK(t *testing.T) {
	for _, tt := range []struct {
		name    string
		reorder bool
	}{
		{"in_order", false},
		{"one_reordered_segment", true},
	} {
		t.Run(tt.name, func(t *testing.T) {
			clock := faketime.NewManualClock()
			c := context.NewWithOpts(t, context.Options{EnableV4: true, MTU: mtu, Clock: clock})
			defer c.Cleanup()
			data := make([]byte, 6*maxPayload)
			ack := sendSpuriousRTOFlight(t, c, clock, data, len(data), false /* rack */)
			ack(maxPayload)
			if err := testutil.Poll(func() error {
				if c.Stack().Stats().TCP.SpuriousRTORecovery.Value() != 1 {
					return fmt.Errorf("first ACK did not establish a spurious timeout")
				}
				return nil
			}, time.Second); err != nil {
				t.Fatal(err)
			}
			info := func() tcpip.TCPInfoOption {
				t.Helper()
				var result tcpip.TCPInfoOption
				if err := c.EP.GetSockOpt(&result); err != nil {
					t.Fatal(err)
				}
				return result
			}
			wantState := func(want tcpip.CongestionControlState) {
				t.Helper()
				if err := testutil.Poll(func() error {
					if got := info().CcState; got != want {
						return fmt.Errorf("congestion state = %v, want %v", got, want)
					}
					return nil
				}, time.Second); err != nil {
					t.Fatal(err)
				}
			}
			if tt.reorder {
				// Segment 3 arrives before segment 2. One SACK reports reordering
				// without establishing a genuine loss.
				ack(maxPayload, header.SACKBlock{
					Start: c.IRS.Add(1 + 2*maxPayload), End: c.IRS.Add(1 + 3*maxPayload),
				})
				wantState(tcpip.Disorder)
			}
			ack(len(data))
			wantState(tcpip.Open)
			got := info().SndSsthresh
			t.Logf("threshold after cumulative ACK = %d", got)
			initialThreshold := tcp.InitialSsthresh
			if want := uint32(initialThreshold); got != want {
				t.Errorf("threshold after spurious timeout = %d, want %d", got, want)
			}
			if n := c.Stack().Stats().TCP.FastRetransmit.Value(); n != 0 {
				t.Errorf("unexpected fast retransmissions: %d", n)
			}
		})
	}
}
