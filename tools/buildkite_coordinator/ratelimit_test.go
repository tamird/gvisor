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

package main

import (
	"maps"
	"reflect"
	"testing"
	"time"
)

func TestQuotaAdmission(t *testing.T) {
	now := time.Date(2026, 11, 28, 12, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name                      string
		today, yesterday, request int64
		allowed                   bool
		exceeded                  uint
	}{
		{"within-both", 40, 50, 50, true, 0},
		{"daily", 90, 0, 20, false, 1},
		{"weekly", 40, 100, 20, false, 7},
		{"both", 90, 50, 20, false, 7},
		{"already-covered", 110, 100, 0, true, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			doc := quotaDoc{MinutesPerDay: map[string]int64{"2026-11-28": tc.today, "2026-11-27": tc.yesterday}}
			before := maps.Clone(doc.MinutesPerDay)
			allowed, days, _ := doc.reserve(now, tc.request, map[uint]uint{1: 100, 7: 150}, 7)
			if allowed != tc.allowed || days != tc.exceeded {
				t.Fatalf("reserve = (%v, %d), want (%v, %d)", allowed, days, tc.allowed, tc.exceeded)
			}
			if !allowed && !reflect.DeepEqual(doc.MinutesPerDay, before) {
				t.Fatalf("denial changed quota: %v", doc.MinutesPerDay)
			}
		})
	}
}

func reserveForTest(t *testing.T, vm *vmAllocation, doc *quotaDoc, id string, now, expiry time.Time) {
	t.Helper()
	if err := vm.prepare(id, now, expiry, 30); err != nil {
		t.Fatal(err)
	}
	if ok, _, _ := doc.reserve(now, vm.Pending.Minutes, map[uint]uint{1: 36000, 30: 360000}, 30); !ok {
		t.Fatal("unexpected quota refusal")
	}
}

func TestVMReuseAndRelease(t *testing.T) {
	now := time.Date(2026, 11, 28, 12, 0, 0, 0, time.UTC)
	var vm vmAllocation
	var doc quotaDoc
	reserveForTest(t, &vm, &doc, "first", now, now.Add(time.Hour))
	if err := vm.activate("first"); err != nil {
		t.Fatal(err)
	}
	reserveForTest(t, &vm, &doc, "failed-pin", now.Add(10*time.Minute), now.Add(70*time.Minute))
	if got := vm.Pending.Minutes; got != 10 {
		t.Fatalf("reuse extension = %d minutes, want 10", got)
	}
	if changed, err := vm.cancel(&doc, "failed-pin"); !changed || err != nil {
		t.Fatalf("cancel = (%v, %v)", changed, err)
	}
	if changed, err := vm.cancel(&doc, "failed-pin"); changed || err != nil {
		t.Fatalf("repeated cancel = (%v, %v)", changed, err)
	}
	if err := vm.activate("failed-pin"); err == nil {
		t.Fatal("canceled grant activated")
	}
	if got := doc.MinutesPerDay["2026-11-28"]; got != 60 {
		t.Fatalf("failed-pin debit = %d, want 60", got)
	}
	reserveForTest(t, &vm, &doc, "reuse", now.Add(20*time.Minute), now.Add(80*time.Minute))
	if err := vm.activate("reuse"); err != nil {
		t.Fatal(err)
	}
	if len(vm.Debits) != 1 || vm.Pending != nil {
		t.Fatalf("reuse retained historical state: %+v", vm)
	}
	if changed, err := vm.cancel(&doc, "reuse"); changed || err != nil {
		t.Fatalf("active grant canceled: (%v, %v)", changed, err)
	}
	if err := vm.settle(&doc, now.Add(25*time.Minute+time.Second)); err != nil {
		t.Fatal(err)
	}
	if got := doc.MinutesPerDay["2026-11-28"]; got != 26 {
		t.Fatalf("settled reused VM = %d minutes, want 26", got)
	}
}

func TestReleaseObservationBounds(t *testing.T) {
	now := time.Date(2026, 11, 28, 12, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name      string
		elapsed   time.Duration
		want      int64
		wantError bool
	}{
		{"early-release", 5 * time.Minute, 5, false},
		{"rounded-once", 5*time.Minute + time.Nanosecond, 6, false},
		{"late-observation-no-debt", 8 * time.Hour, 60, false},
		{"backward-clock", -time.Second, 60, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var vm vmAllocation
			var doc quotaDoc
			reserveForTest(t, &vm, &doc, "grant", now, now.Add(time.Hour))
			if err := vm.activate("grant"); err != nil {
				t.Fatal(err)
			}
			if err := vm.settle(&doc, now.Add(tc.elapsed)); (err != nil) != tc.wantError {
				t.Fatalf("settle error = %v", err)
			}
			if got := doc.MinutesPerDay["2026-11-28"]; got != tc.want {
				t.Fatalf("settled minutes = %d, want %d", got, tc.want)
			}
		})
	}
}

func TestDebitDayRefunds(t *testing.T) {
	now := time.Date(2026, 11, 27, 23, 50, 0, 0, time.UTC)
	var vm vmAllocation
	var doc quotaDoc
	reserveForTest(t, &vm, &doc, "first", now, now.Add(time.Hour))
	if err := vm.activate("first"); err != nil {
		t.Fatal(err)
	}
	reserveForTest(t, &vm, &doc, "next-day", now.Add(10*time.Minute), now.Add(70*time.Minute))
	if err := vm.activate("next-day"); err != nil {
		t.Fatal(err)
	}
	if err := vm.settle(&doc, now.Add(25*time.Minute)); err != nil {
		t.Fatal(err)
	}
	want := map[string]int64{"2026-11-27": 25, "2026-11-28": 0}
	if !reflect.DeepEqual(doc.MinutesPerDay, want) {
		t.Fatalf("debit days = %v, want %v", doc.MinutesPerDay, want)
	}
	delete(doc.MinutesPerDay, "2026-11-27")
	if err := doc.refund("2026-11-27", 1); err != nil {
		t.Fatal(err)
	}
	if _, ok := doc.MinutesPerDay["2026-11-27"]; ok {
		t.Fatal("refund recreated a pruned day")
	}
}

func TestExpiredGrantDoesNotChargeObservationGap(t *testing.T) {
	now := time.Date(2026, 11, 28, 12, 0, 0, 0, time.UTC)
	var vm vmAllocation
	var doc quotaDoc
	reserveForTest(t, &vm, &doc, "first", now, now.Add(time.Hour))
	if err := vm.activate("first"); err != nil {
		t.Fatal(err)
	}
	later := now.Add(3 * time.Hour)
	reserveForTest(t, &vm, &doc, "after-gap", later, later.Add(time.Hour))
	if got := vm.Pending.Minutes; got != 60 {
		t.Fatalf("new budget = %d, want 60 without gap debt", got)
	}
	if err := vm.activate("after-gap"); err != nil {
		t.Fatal(err)
	}
	if err := vm.settle(&doc, later.Add(5*time.Minute)); err != nil {
		t.Fatal(err)
	}
	if got := doc.MinutesPerDay["2026-11-28"]; got != 65 {
		t.Fatalf("charge = %d, want prior capped60 + new5", got)
	}
}

func TestOwnerMismatchDoesNotConsumeJobRetries(t *testing.T) {
	pool := NewJobPool(1, map[string]struct{}{"default": {}}, nil)
	metadata := BuildMetadata{source: buildSourceWebhook, authorKnown: true, author: 42}
	token := JobAcquisitionToken{attempts: jatMaxAttempts - 1}
	if !pool.add("default", "build", metadata, "job", token) {
		t.Fatal("could not seed last allowed attempt")
	}
	wrongOwner := &untrustedAgent{agentMetadata: agentMetadata{queue: "default"}, buildUUID: "build"}
	for range jatMaxAttempts + 2 {
		job, _ := pool.assignJob(nil, wrongOwner)
		if job == nil || job.jat.attempts != jatMaxAttempts {
			t.Fatalf("wrong-owner selection = %+v", job)
		}
		pool.returnUnattempted(job)
	}
	eligible := &unassignedAgent{agentMetadata: agentMetadata{queue: "default"}}
	job, _ := pool.assignJob(nil, eligible)
	if job == nil || job.jobUUID != "job" || job.jat.attempts != jatMaxAttempts {
		t.Fatalf("eligible handoff = %+v", job)
	}
	if pool.add(job.queue, job.buildUUID, job.metadata, job.jobUUID, job.jat) {
		t.Fatal("ordinary failed attempt exceeded its retry limit")
	}
}
