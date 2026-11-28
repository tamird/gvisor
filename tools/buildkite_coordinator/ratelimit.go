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
	"fmt"
	"slices"
	"strconv"
	"time"

	"cloud.google.com/go/firestore"
	"google.golang.org/api/iterator"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const (
	firestoreTimeout              = 5 * time.Second
	unactivatedReservationTimeout = 5 * time.Minute
)

// rateLimits defines immutable per-author compute-minute limits.
var rateLimits = map[uint]uint{
	1:  36000,
	7:  144000,
	30: 360000,
}

var errAllocationOwner = errors.New("VM allocation belongs to another author")
var errGrantPending = errors.New("VM already has a pending grant")

// quotaDoc preserves the existing per-author accounting format.
type quotaDoc struct {
	MinutesPerDay map[string]int64 `firestore:"minutesPerDay"`
}

// reservedInstance identifies an allocation independently of its reusable name.
type reservedInstance struct {
	Project string
	Zone    string
	ID      string
}

func (id instanceIdentity) reservedInstance() reservedInstance {
	return reservedInstance{Project: id.projectID, Zone: id.zone, ID: id.instanceID}
}

// pendingGrant reserves an extension before pinning or granting a VM timeout.
type pendingGrant struct {
	ID        string
	StartedAt time.Time
	Until     time.Time
	Minutes   int64
}

// vmAllocation coalesces grants for one VM. Documents exist only while there is
// outstanding accounting. Debit-day history is bounded by the quota windows;
// reuse does not append historical job records.
type vmAllocation struct {
	Author        string
	Instance      reservedInstance
	StartedAt     time.Time
	ReservedUntil time.Time
	Debits        map[string]int64
	Pending       *pendingGrant
}

type reservationRef struct {
	author   authorID
	instance reservedInstance
	id       string
}

// RateLimitController stores admission budgets and prospective VM accounting.
// All fields are immutable and the client is safe for concurrent use.
type RateLimitController struct {
	client     *firestore.Client
	limits     map[uint]uint
	maxDays    uint
	collection string
}

func NewRateLimitController(ctx context.Context, project, collection string, limits map[uint]uint) (*RateLimitController, error) {
	var maxDays uint
	for days := range limits {
		if days == 0 {
			return nil, fmt.Errorf("rate limit window must be at least one day")
		}
		maxDays = max(maxDays, days)
	}
	client, err := firestore.NewClient(ctx, project)
	if err != nil {
		return nil, err
	}
	return &RateLimitController{client: client, limits: limits, maxDays: maxDays, collection: collection}, nil
}

func (r *RateLimitController) quotaRef(author authorID) *firestore.DocumentRef {
	return r.client.Collection(r.collection).Doc(strconv.FormatUint(uint64(author), 10))
}

func (r *RateLimitController) instanceRef(id reservedInstance) *firestore.DocumentRef {
	return r.client.Collection(r.collection + "Instances").Doc(id.Project + "-" + id.Zone + "-" + id.ID)
}

func readQuota(ctx context.Context, tx *firestore.Transaction, ref *firestore.DocumentRef) (quotaDoc, error) {
	var doc quotaDoc
	snapshot, err := tx.Get(ref)
	if status.Code(err) == codes.NotFound {
		return doc, nil
	}
	if err != nil {
		return doc, err
	}
	err = snapshot.DataTo(&doc)
	return doc, err
}

func readAllocation(ctx context.Context, tx *firestore.Transaction, ref *firestore.DocumentRef) (vmAllocation, bool, error) {
	var vm vmAllocation
	snapshot, err := tx.Get(ref)
	if status.Code(err) == codes.NotFound {
		return vm, false, nil
	}
	if err != nil {
		return vm, false, err
	}
	err = snapshot.DataTo(&vm)
	return vm, true, err
}

// reserve adds only newly exposed VM time. Refusal does not change the document.
func (doc *quotaDoc) reserve(now time.Time, minutes int64, limits map[uint]uint, maxDays uint) (bool, uint, uint) {
	buckets := make(map[string]int64)
	var total int64
	var exceededDays, exceededQuota uint
	for i := uint(0); i < maxDays; i++ {
		day := now.AddDate(0, 0, -int(i)).Format(time.DateOnly)
		if n, ok := doc.MinutesPerDay[day]; ok {
			buckets[day] = n
			total += n
		}
		if limit, ok := limits[i+1]; ok && total+minutes > int64(limit) {
			exceededDays, exceededQuota = i+1, limit
		}
	}
	if minutes != 0 && exceededDays != 0 {
		return false, exceededDays, exceededQuota
	}
	buckets[now.Format(time.DateOnly)] += minutes
	doc.MinutesPerDay = buckets
	return true, 0, 0
}

func (doc *quotaDoc) refund(day string, minutes int64) error {
	if n, ok := doc.MinutesPerDay[day]; ok {
		if n < minutes {
			return fmt.Errorf("refund of %d exceeds debit of %d on %s", minutes, n, day)
		}
		doc.MinutesPerDay[day] -= minutes
	}
	// An expired bucket is outside all quota windows. Do not recreate it.
	return nil
}

func (vm *vmAllocation) prepare(id string, now, expiry time.Time, maxDays uint) error {
	if vm.Pending != nil {
		return errGrantPending
	}
	if !expiry.After(now) {
		return fmt.Errorf("reservation expiry must follow its start")
	}
	start, until := now, expiry
	var previous int64
	newInterval := vm.StartedAt.IsZero() || !now.Before(vm.ReservedUntil)
	if !newInterval {
		start = vm.StartedAt
		if vm.ReservedUntil.After(expiry) {
			until = vm.ReservedUntil
		}
		previous = toMinutes(vm.ReservedUntil.Sub(start))
	}
	// Once a previous deadline has passed, retain its full charge but do not
	// manufacture additional debt for an unobserved gap before this grant.
	vm.Pending = &pendingGrant{ID: id, StartedAt: now, Until: until, Minutes: toMinutes(until.Sub(start)) - previous}
	cutoff := now.AddDate(0, 0, -int(maxDays-1)).Format(time.DateOnly)
	for day := range vm.Debits {
		if day < cutoff {
			delete(vm.Debits, day)
		}
	}
	return nil
}

// ReserveTime atomically reserves quota and one pending VM grant before pinning.
// A nil reference means the author is rate-limited. Serialization is per VM, not
// a limit on an author's jobs or VMs.
func (r *RateLimitController) ReserveTime(ctx context.Context, author authorID, instance instanceIdentity, now, expiry time.Time) (*reservationRef, uint, uint, error) {
	ctx, cancel := context.WithTimeout(ctx, firestoreTimeout)
	defer cancel()
	ref := &reservationRef{author: author, instance: instance.reservedInstance(), id: rand.Text()}
	authorRef, instanceRef := r.quotaRef(author), r.instanceRef(ref.instance)
	var allowed bool
	var days, quota uint
	err := r.client.RunTransaction(ctx, func(ctx context.Context, tx *firestore.Transaction) error {
		allowed, days, quota = false, 0, 0
		vm, exists, err := readAllocation(ctx, tx, instanceRef)
		if err != nil {
			return err
		}
		authorString := authorRef.ID
		if exists && vm.Instance != ref.instance {
			return fmt.Errorf("VM allocation identity mismatch")
		}
		if exists && vm.Pending != nil {
			return errGrantPending
		}
		if exists && vm.Author != authorString {
			return errAllocationOwner
		}
		if !exists {
			vm = vmAllocation{Author: authorString, Instance: ref.instance}
		}
		if err := vm.prepare(ref.id, now.UTC(), expiry.UTC(), r.maxDays); err != nil {
			return err
		}
		doc, err := readQuota(ctx, tx, authorRef)
		if err != nil {
			return err
		}
		allowed, days, quota = doc.reserve(now.UTC(), vm.Pending.Minutes, r.limits, r.maxDays)
		if !allowed {
			return nil
		}
		if err := tx.Set(authorRef, doc); err != nil {
			return err
		}
		return tx.Set(instanceRef, vm)
	})
	if err != nil || !allowed {
		return nil, days, quota, err
	}
	return ref, 0, 0, nil
}

func (vm *vmAllocation) activate(id string) error {
	if vm.Pending == nil || vm.Pending.ID != id {
		return fmt.Errorf("reservation no longer exists")
	}
	if vm.StartedAt.IsZero() || !vm.Pending.StartedAt.Before(vm.ReservedUntil) {
		vm.StartedAt = vm.Pending.StartedAt
		vm.Debits = make(map[string]int64)
	}
	vm.ReservedUntil = vm.Pending.Until
	if vm.Debits == nil {
		vm.Debits = make(map[string]int64)
	}
	vm.Debits[vm.Pending.StartedAt.Format(time.DateOnly)] += vm.Pending.Minutes
	vm.Pending = nil
	return nil
}

// Activate must succeed durably before extending VM capability or handing out a
// job. A canceled pending ID can never subsequently activate.
func (r *RateLimitController) Activate(ctx context.Context, ref *reservationRef) error {
	ctx, cancel := context.WithTimeout(ctx, firestoreTimeout)
	defer cancel()
	instanceRef := r.instanceRef(ref.instance)
	return r.client.RunTransaction(ctx, func(ctx context.Context, tx *firestore.Transaction) error {
		vm, exists, err := readAllocation(ctx, tx, instanceRef)
		if err != nil {
			return err
		}
		if !exists || vm.Author != r.quotaRef(ref.author).ID {
			return fmt.Errorf("allocation no longer exists for author")
		}
		if err := vm.activate(ref.id); err != nil {
			return err
		}
		return tx.Set(instanceRef, vm)
	})
}

func (vm *vmAllocation) cancel(doc *quotaDoc, id string) (bool, error) {
	if vm.Pending == nil || vm.Pending.ID != id {
		return false, nil
	}
	if err := doc.refund(vm.Pending.StartedAt.Format(time.DateOnly), vm.Pending.Minutes); err != nil {
		return false, err
	}
	vm.Pending = nil
	return true, nil
}

// Cancel refunds only the matching pending extension, at most once. Activated
// time remains reserved even if delivery of the job failed.
func (r *RateLimitController) Cancel(ctx context.Context, ref *reservationRef) error {
	ctx, cancel := context.WithTimeout(ctx, firestoreTimeout)
	defer cancel()
	authorRef, instanceRef := r.quotaRef(ref.author), r.instanceRef(ref.instance)
	return r.client.RunTransaction(ctx, func(ctx context.Context, tx *firestore.Transaction) error {
		vm, exists, err := readAllocation(ctx, tx, instanceRef)
		if err != nil {
			return err
		}
		if !exists || vm.Author != authorRef.ID || vm.Pending == nil || vm.Pending.ID != ref.id {
			return nil
		}
		doc, err := readQuota(ctx, tx, authorRef)
		if err != nil {
			return err
		}
		if _, err := vm.cancel(&doc, ref.id); err != nil {
			return err
		}
		if err := tx.Set(authorRef, doc); err != nil {
			return err
		}
		if vm.StartedAt.IsZero() {
			return tx.Delete(instanceRef)
		}
		return tx.Set(instanceRef, vm)
	})
}

// settle refunds unused tail capacity. A late absence observation is only an
// upper bound on release; it never increases the original reserved charge.
func (vm *vmAllocation) settle(doc *quotaDoc, goneAt time.Time) error {
	if !vm.StartedAt.IsZero() {
		if goneAt.Before(vm.StartedAt) {
			return fmt.Errorf("release observation precedes reservation")
		}
		reserved := toMinutes(vm.ReservedUntil.Sub(vm.StartedAt))
		unused := max(int64(0), reserved-toMinutes(goneAt.Sub(vm.StartedAt)))
		days := make([]string, 0, len(vm.Debits))
		for day := range vm.Debits {
			days = append(days, day)
		}
		slices.Sort(days)
		slices.Reverse(days)
		for _, day := range days {
			refund := min(unused, vm.Debits[day])
			if err := doc.refund(day, refund); err != nil {
				return err
			}
			unused -= refund
		}
	}
	if vm.Pending != nil {
		return doc.refund(vm.Pending.StartedAt.Format(time.DateOnly), vm.Pending.Minutes)
	}
	return nil
}

func (r *RateLimitController) settle(ctx context.Context, instance reservedInstance, goneAt time.Time) error {
	ctx, cancel := context.WithTimeout(ctx, firestoreTimeout)
	defer cancel()
	instanceRef := r.instanceRef(instance)
	return r.client.RunTransaction(ctx, func(ctx context.Context, tx *firestore.Transaction) error {
		vm, exists, err := readAllocation(ctx, tx, instanceRef)
		if err != nil || !exists {
			return err
		}
		author, err := parseAuthorID(vm.Author)
		if err != nil {
			return err
		}
		authorRef := r.quotaRef(author)
		doc, err := readQuota(ctx, tx, authorRef)
		if err != nil {
			return err
		}
		if err := vm.settle(&doc, goneAt); err != nil {
			return err
		}
		if err := tx.Set(authorRef, doc); err != nil {
			return err
		}
		return tx.Delete(instanceRef)
	})
}

// Reconcile scans only outstanding VM documents. The existing inventory selects
// possible releases, then gone must confirm exact-instance absence. No Buildkite
// completion notice or guest timestamp can release active quota.
func (r *RateLimitController) Reconcile(ctx context.Context, inventory map[reservedInstance]struct{}, gone func(context.Context, reservedInstance) (time.Time, error)) error {
	iter := r.client.Collection(r.collection + "Instances").Documents(ctx)
	defer iter.Stop()
	var errs []error
	for {
		snapshot, err := iter.Next()
		if err == iterator.Done {
			return errors.Join(errs...)
		}
		if err != nil {
			return errors.Join(append(errs, err)...)
		}
		var vm vmAllocation
		if err := snapshot.DataTo(&vm); err != nil {
			errs = append(errs, err)
			continue
		}
		if vm.Pending != nil && time.Since(vm.Pending.StartedAt) > unactivatedReservationTimeout {
			author, err := parseAuthorID(vm.Author)
			if err != nil {
				errs = append(errs, err)
				continue
			}
			if err := r.Cancel(ctx, &reservationRef{author: author, instance: vm.Instance, id: vm.Pending.ID}); err != nil {
				errs = append(errs, err)
			}
		}
		if _, live := inventory[vm.Instance]; live {
			continue
		}
		goneAt, err := gone(ctx, vm.Instance)
		if err != nil {
			errs = append(errs, err)
			continue
		}
		if !goneAt.IsZero() {
			if err := r.settle(ctx, vm.Instance, goneAt); err != nil {
				errs = append(errs, err)
			}
		}
	}
}

// toMinutes rounds nonnegative durations up to the next minute.
func toMinutes(d time.Duration) int64 { return int64((d + time.Minute - 1) / time.Minute) }
