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

package tcp

import (
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/sleep"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
)

func TestCleanup(t *testing.T) {
	const (
		timerDurationSeconds     = 2
		isAssertedTimeoutSeconds = timerDurationSeconds + 1
	)

	clock := faketime.NewManualClock()

	tmr := timer{}
	w := sleep.Waker{}
	tmr.init(clock, w.Assert)
	tmr.enable(timerDurationSeconds * time.Second)
	tmr.cleanup()

	if !tmr.isUninitialized() {
		t.Errorf("got tmr.isUninitialized = false, want = true")
	}

	// The waker should not be asserted.
	for i := 0; i < isAssertedTimeoutSeconds; i++ {
		clock.Advance(time.Second)
		if w.IsAsserted() {
			t.Fatalf("waker asserted unexpectedly")
		}
	}
}

func TestRetryTimeoutBudget(t *testing.T) {
	for _, tc := range []struct {
		name           string
		minRTO, maxRTO time.Duration
		retries        uint32
		budget         time.Duration
	}{
		{"Default", MinRTO, MaxRTO, 15, 924600 * time.Millisecond},
		{"NoLowerClamp", 0, MaxRTO, 15, 924600 * time.Millisecond},
		{"ShortUpperClamp", 0, 50 * time.Millisecond, 2, 150 * time.Millisecond},
		{"HigherLowerClamp", time.Second, MaxRTO, 2, 7 * time.Second},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if retriesTimedOut(tc.budget-1, tc.minRTO, tc.maxRTO, tc.retries) || !retriesTimedOut(tc.budget, tc.minRTO, tc.maxRTO, tc.retries) {
				t.Errorf("retry budget differs from %s", tc.budget)
			}
		})
	}
	if retriesTimedOut(time.Duration(1<<63-1), MinRTO, MaxRTO, ^uint32(0)) {
		t.Error("retry budget overflowed")
	}
}
