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

func TestRenoCongestionAvoidanceConsumesACKCredit(t *testing.T) {
	r := newRenoCC(&sender{
		ep: &Endpoint{},
		TCPSenderState: TCPSenderState{
			SndCwnd:  10,
			Ssthresh: 10,
		},
	})
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
}
