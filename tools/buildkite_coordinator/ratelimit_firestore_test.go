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
	"context"
	"crypto/rand"
	"errors"
	"net"
	"os"
	"sync"
	"testing"
	"time"

	"google.golang.org/api/iterator"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func newFirestoreQuota(t *testing.T) *RateLimitController {
	t.Helper()
	endpoint := os.Getenv("FIRESTORE_EMULATOR_HOST")
	if endpoint == "" {
		t.Skip("requires a local Firestore emulator")
	}
	host, _, err := net.SplitHostPort(endpoint)
	if err != nil || (host != "localhost" && !net.ParseIP(host).IsLoopback()) {
		t.Fatal("Firestore tests require a loopback emulator")
	}
	r, err := NewRateLimitController(context.Background(), "test-project", "quota-"+rand.Text(), map[uint]uint{1: 180})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		for _, collection := range []string{r.collection, r.collection + "Instances"} {
			iter := r.client.Collection(collection).Documents(ctx)
			for {
				doc, err := iter.Next()
				if err == iterator.Done {
					break
				}
				if err != nil {
					t.Errorf("cleanup query: %v", err)
					break
				}
				if _, err := doc.Ref.Delete(ctx); err != nil {
					t.Errorf("cleanup delete: %v", err)
				}
			}
			iter.Stop()
		}
		r.client.Close()
	})
	return r
}

func readMinutes(t *testing.T, r *RateLimitController, author authorID, day string) int64 {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), firestoreTimeout)
	defer cancel()
	snapshot, err := r.quotaRef(author).Get(ctx)
	if status.Code(err) == codes.NotFound {
		return 0
	}
	if err != nil {
		t.Fatal(err)
	}
	var doc quotaDoc
	if err := snapshot.DataTo(&doc); err != nil {
		t.Fatal(err)
	}
	return doc.MinutesPerDay[day]
}

func TestFirestoreRestartAndProviderConfirmation(t *testing.T) {
	r := newFirestoreQuota(t)
	ctx := context.Background()
	now := time.Date(2026, 11, 28, 12, 0, 0, 0, time.UTC)
	instance := instanceIdentity{projectID: "test-project", zone: "zone", instanceID: "123"}
	ref, _, _, err := r.ReserveTime(ctx, 42, instance, now, now.Add(time.Hour))
	if err != nil || ref == nil {
		t.Fatalf("reserve = %v, %v", ref, err)
	}
	if err := r.Activate(ctx, ref); err != nil {
		t.Fatal(err)
	}
	restarted, err := NewRateLimitController(ctx, "test-project", r.collection, r.limits)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { restarted.client.Close() })
	r = restarted
	if err := r.Cancel(ctx, ref); err != nil {
		t.Fatal(err)
	}
	if got := readMinutes(t, r, 42, "2026-11-28"); got != 60 {
		t.Fatalf("active cancel changed debit to %d", got)
	}
	inventory := map[reservedInstance]struct{}{instance.reservedInstance(): {}}
	if err := r.Reconcile(ctx, inventory, func(context.Context, reservedInstance) (time.Time, error) {
		t.Fatal("queried a VM present in inventory")
		return time.Time{}, nil
	}); err != nil {
		t.Fatal(err)
	}
	unknown := func(context.Context, reservedInstance) (time.Time, error) {
		return time.Time{}, errors.New("provider unavailable")
	}
	if err := r.Reconcile(ctx, nil, unknown); err == nil {
		t.Fatal("provider error ignored")
	}
	if got := readMinutes(t, r, 42, "2026-11-28"); got != 60 {
		t.Fatalf("unknown release refunded debit: %d", got)
	}
	gone := func(context.Context, reservedInstance) (time.Time, error) { return now.Add(10 * time.Minute), nil }
	if err := r.Reconcile(ctx, nil, gone); err != nil {
		t.Fatal(err)
	}
	if err := r.Reconcile(ctx, nil, gone); err != nil {
		t.Fatal(err)
	}
	if got := readMinutes(t, r, 42, "2026-11-28"); got != 10 {
		t.Fatalf("settled debit = %d, want 10", got)
	}
}

func TestFirestorePendingCancellationAndOwner(t *testing.T) {
	r := newFirestoreQuota(t)
	ctx := context.Background()
	now := time.Date(2026, 11, 28, 12, 0, 0, 0, time.UTC)
	instance := instanceIdentity{projectID: "test-project", zone: "zone", instanceID: "123"}
	ref, _, _, err := r.ReserveTime(ctx, 0, instance, now, now.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if next, _, _, err := r.ReserveTime(ctx, 42, instance, now, now.Add(time.Hour)); !errors.Is(err, errGrantPending) || next != nil {
		t.Fatalf("competing unpinned grant = %v, %v", next, err)
	}
	if err := r.Cancel(ctx, ref); err != nil {
		t.Fatal(err)
	}
	if err := r.Cancel(ctx, ref); err != nil {
		t.Fatal(err)
	}
	if err := r.Activate(ctx, ref); err == nil {
		t.Fatal("canceled grant activated")
	}
	ref, _, _, err = r.ReserveTime(ctx, 0, instance, now, now.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Activate(ctx, ref); err != nil {
		t.Fatal(err)
	}
	if next, _, _, err := r.ReserveTime(ctx, 42, instance, now, now.Add(time.Hour)); !errors.Is(err, errAllocationOwner) || next != nil {
		t.Fatalf("owner transition = %v, %v", next, err)
	}
	if got := readMinutes(t, r, 42, "2026-11-28"); got != 0 {
		t.Fatalf("rejected owner charged %d", got)
	}
	instance.instanceID = "456"
	next, _, _, err := r.ReserveTime(ctx, 42, instance, now, now.Add(time.Hour))
	if err != nil || next == nil {
		t.Fatalf("fresh VM admission = %v, %v", next, err)
	}
	if got := readMinutes(t, r, 42, "2026-11-28"); got != 60 {
		t.Fatalf("fresh VM charge = %d", got)
	}
	if err := r.Cancel(ctx, next); err != nil {
		t.Fatal(err)
	}
	if got := readMinutes(t, r, 42, "2026-11-28"); got != 0 {
		t.Fatalf("failed-pin charge retained: %d", got)
	}
}

func TestFirestoreConcurrentAdmission(t *testing.T) {
	for _, tc := range []struct {
		name          string
		ids           []string
		limit         uint
		pendingErrors int
	}{
		{"author-quota", []string{"123", "456"}, 90, 0},
		{"same-VM", []string{"123", "123"}, 180, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := newFirestoreQuota(t)
			r.limits = map[uint]uint{1: tc.limit}
			now := time.Date(2026, 11, 28, 12, 0, 0, 0, time.UTC)
			type result struct {
				ref *reservationRef
				err error
			}
			results := make(chan result, 2)
			var wg sync.WaitGroup
			start := make(chan struct{})
			for _, id := range tc.ids {
				wg.Go(func() {
					<-start
					ref, _, _, err := r.ReserveTime(context.Background(), 42, instanceIdentity{projectID: "test-project", zone: "zone", instanceID: id}, now, now.Add(time.Hour))
					results <- result{ref, err}
				})
			}
			close(start)
			wg.Wait()
			close(results)
			admitted, pending := 0, 0
			for result := range results {
				if result.ref != nil {
					admitted++
				}
				if errors.Is(result.err, errGrantPending) {
					pending++
				} else if result.err != nil {
					t.Error(result.err)
				}
			}
			if admitted != 1 || pending != tc.pendingErrors || readMinutes(t, r, 42, "2026-11-28") != 60 {
				t.Fatalf("concurrent admissions=%d pending=%d, want one60-minute debit and%d pending errors", admitted, pending, tc.pendingErrors)
			}
		})
	}
}

func TestFirestoreConcurrentActivationCancellation(t *testing.T) {
	r := newFirestoreQuota(t)
	ctx := context.Background()
	now := time.Now()
	instance := instanceIdentity{projectID: "test-project", zone: "zone", instanceID: "123"}
	ref, _, _, err := r.ReserveTime(ctx, 42, instance, now, now.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	var activationErr, cancelErr error
	var wg sync.WaitGroup
	start := make(chan struct{})
	wg.Go(func() { <-start; activationErr = r.Activate(ctx, ref) })
	wg.Go(func() { <-start; cancelErr = r.Cancel(ctx, ref) })
	close(start)
	wg.Wait()
	if cancelErr != nil {
		t.Fatal(cancelErr)
	}
	want := int64(60)
	if activationErr != nil {
		snapshot, err := r.instanceRef(instance.reservedInstance()).Get(ctx)
		if status.Code(err) == codes.NotFound {
			want = 0
		} else if err != nil {
			t.Fatal(err)
		} else {
			var vm vmAllocation
			if err := snapshot.DataTo(&vm); err != nil {
				t.Fatal(err)
			}
			if vm.StartedAt.IsZero() || vm.Pending != nil {
				t.Fatalf("activation failed but allocation was neither canceled nor activated: %+v", vm)
			}
		}
	}
	if got := readMinutes(t, r, 42, now.UTC().Format(time.DateOnly)); got != want {
		t.Fatalf("activation error %v with debit %d, want %d", activationErr, got, want)
	}
}

func TestFirestoreConcurrentSettlementAdmission(t *testing.T) {
	r := newFirestoreQuota(t)
	ctx := context.Background()
	now := time.Date(2026, 11, 28, 12, 0, 0, 0, time.UTC)
	first := instanceIdentity{projectID: "test-project", zone: "zone", instanceID: "123"}
	ref, _, _, err := r.ReserveTime(ctx, 42, first, now, now.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Activate(ctx, ref); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	start := make(chan struct{})
	wg.Go(func() {
		<-start
		if err := r.settle(ctx, first.reservedInstance(), now.Add(10*time.Minute)); err != nil {
			t.Error(err)
		}
	})
	wg.Go(func() {
		<-start
		ref, _, _, err := r.ReserveTime(ctx, 42, instanceIdentity{projectID: "test-project", zone: "zone", instanceID: "456"}, now, now.Add(time.Hour))
		if err != nil || ref == nil {
			t.Errorf("reserve: %v, %v", ref, err)
		}
	})
	close(start)
	wg.Wait()
	if got := readMinutes(t, r, 42, "2026-11-28"); got != 70 {
		t.Fatalf("settlement/admission debit = %d, want 10+60", got)
	}
}

func TestFirestoreRestartCancelsUnactivatedGrant(t *testing.T) {
	r := newFirestoreQuota(t)
	ctx := context.Background()
	now := time.Now().Add(-10 * time.Minute)
	instance := instanceIdentity{projectID: "test-project", zone: "zone", instanceID: "123"}
	ref, _, _, err := r.ReserveTime(ctx, 42, instance, now, now.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	restarted, err := NewRateLimitController(ctx, "test-project", r.collection, r.limits)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { restarted.client.Close() })
	if err := restarted.Reconcile(ctx, map[reservedInstance]struct{}{instance.reservedInstance(): {}}, func(context.Context, reservedInstance) (time.Time, error) {
		t.Fatal("queried a VM in inventory")
		return time.Time{}, nil
	}); err != nil {
		t.Fatal(err)
	}
	if err := r.Activate(ctx, ref); err == nil {
		t.Fatal("orphan canceled on restart could still grant capability")
	}
	if got := readMinutes(t, r, 42, now.UTC().Format(time.DateOnly)); got != 0 {
		t.Fatalf("orphan retained debit %d", got)
	}
}
