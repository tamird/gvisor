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
	"testing"

	"gvisor.dev/gvisor/pkg/tcpip"
)

func newTestReno() *renoState {
	return newRenoCC(&sender{
		ep: &Endpoint{},
		senderState: senderState{
			SndCwnd:  10,
			Ssthresh: 10,
		},
	})
}

func TestRenoCongestionAvoidanceConsumesACKCredit(t *testing.T) {
	r := newTestReno()
	r.s.ep.mu.Lock()
	defer r.s.ep.mu.Unlock()
	r.Update(10, 0, tcpip.MonotonicTime{})
	if got := r.s.SndCwnd; got != 11 {
		t.Fatalf("cwnd after acknowledging the first window = %d, want 11", got)
	}
	// Ten more segments do not yet acknowledge another full window.
	for range 10 {
		r.Update(1, 0, tcpip.MonotonicTime{})
	}
	if got := r.s.SndCwnd; got != 11 {
		t.Fatalf("cwnd after acknowledging ten more segments = %d, want 11", got)
	}
	r.Update(1, 0, tcpip.MonotonicTime{})
	if got := r.s.SndCwnd; got != 12 {
		t.Fatalf("cwnd after acknowledging the second window = %d, want 12", got)
	}
}

func TestRenoCongestionAvoidanceCarriesExcessACKCredit(t *testing.T) {
	r := newTestReno()
	r.s.ep.mu.Lock()
	defer r.s.ep.mu.Unlock()
	for _, step := range []struct {
		acked int
		want  int
	}{
		{acked: 9, want: 10},
		{acked: 2, want: 11},
		{acked: 10, want: 12},
	} {
		r.Update(step.acked, 0, tcpip.MonotonicTime{})
		if got := r.s.SndCwnd; got != step.want {
			t.Fatalf("cwnd after acknowledging %d segments = %d, want %d", step.acked, got, step.want)
		}
	}
}
