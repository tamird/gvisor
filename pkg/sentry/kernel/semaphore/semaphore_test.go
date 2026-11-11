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

package semaphore

import (
	"testing"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/errors/linuxerr"
	"gvisor.dev/gvisor/pkg/sentry/contexttest"
	"gvisor.dev/gvisor/pkg/sentry/kernel/auth"
	"gvisor.dev/gvisor/pkg/sentry/kernel/ipc"
)

func executeOps(ctx context.Context, t *testing.T, set *Set, ops []linux.Sembuf, block bool) chan struct{} {
	set.mu.Lock()
	ch, _, err := set.executeOps(ctx, ops, 123)
	set.mu.Unlock()
	if err != nil {
		t.Fatalf("ExecuteOps(ops) failed, err: %v, ops: %+v", err, ops)
	}
	if block {
		if ch == nil {
			t.Fatalf("ExecuteOps(ops) got: nil, expected: !nil, ops: %+v", ops)
		}
		if signalled(ch) {
			t.Fatalf("ExecuteOps(ops) channel should not have been signalled, ops: %+v", ops)
		}
	} else {
		if ch != nil {
			t.Fatalf("ExecuteOps(ops) got: %v, expected: nil, ops: %+v", ch, ops)
		}
	}
	return ch
}

func signalled(ch chan struct{}) bool {
	select {
	case <-ch:
		return true
	default:
		return false
	}
}

func TestBasic(t *testing.T) {
	ctx := contexttest.Context(t)
	set := &Set{obj: &ipc.Object{ID: 123}, sems: make([]sem, 1)}
	ops := []linux.Sembuf{
		{SemOp: 1},
	}
	executeOps(ctx, t, set, ops, false)

	ops[0].SemOp = -1
	executeOps(ctx, t, set, ops, false)

	ops[0].SemOp = -1
	ch1 := executeOps(ctx, t, set, ops, true)

	ops[0].SemOp = 1
	executeOps(ctx, t, set, ops, false)
	if !signalled(ch1) {
		t.Fatalf("ExecuteOps(ops) channel should not have been signalled, ops: %+v", ops)
	}
}

func TestWaitForZero(t *testing.T) {
	ctx := contexttest.Context(t)
	set := &Set{obj: &ipc.Object{ID: 123}, sems: make([]sem, 1)}
	ops := []linux.Sembuf{
		{SemOp: 0},
	}
	executeOps(ctx, t, set, ops, false)

	ops[0].SemOp = -2
	ch1 := executeOps(ctx, t, set, ops, true)

	ops[0].SemOp = 0
	executeOps(ctx, t, set, ops, false)

	ops[0].SemOp = 1
	executeOps(ctx, t, set, ops, false)

	ops[0].SemOp = 0
	chZero1 := executeOps(ctx, t, set, ops, true)

	ops[0].SemOp = 0
	chZero2 := executeOps(ctx, t, set, ops, true)

	ops[0].SemOp = 1
	executeOps(ctx, t, set, ops, false)
	if !signalled(ch1) {
		t.Fatalf("ExecuteOps(ops) channel should have been signalled, ops: %+v, set: %+v", ops, set)
	}

	ops[0].SemOp = -2
	executeOps(ctx, t, set, ops, false)
	if !signalled(chZero1) {
		t.Fatalf("ExecuteOps(ops) channel zero 1 should have been signalled, ops: %+v, set: %+v", ops, set)
	}
	if !signalled(chZero2) {
		t.Fatalf("ExecuteOps(ops) channel zero 2 should have been signalled, ops: %+v, set: %+v", ops, set)
	}
}

func TestNoWait(t *testing.T) {
	ctx := contexttest.Context(t)
	set := &Set{obj: &ipc.Object{ID: 123}, sems: make([]sem, 1)}
	ops := []linux.Sembuf{
		{SemOp: 1},
	}
	executeOps(ctx, t, set, ops, false)

	ops[0].SemOp = -2
	ops[0].SemFlg = linux.IPC_NOWAIT
	set.mu.Lock()
	defer set.mu.Unlock()
	if _, _, err := set.executeOps(ctx, ops, 123); err != linuxerr.ErrWouldBlock {
		t.Fatalf("ExecuteOps(ops) wrong result, got: %v, expected: %v", err, linuxerr.ErrWouldBlock)
	}

	ops[0].SemOp = 0
	ops[0].SemFlg = linux.IPC_NOWAIT
	if _, _, err := set.executeOps(ctx, ops, 123); err != linuxerr.ErrWouldBlock {
		t.Fatalf("ExecuteOps(ops) wrong result, got: %v, expected: %v", err, linuxerr.ErrWouldBlock)
	}
}

func TestUnregister(t *testing.T) {
	ctx := contexttest.Context(t)
	r := NewRegistry(auth.NewRootUserNamespace())
	set, err := r.FindOrCreate(ctx, 123, 2, linux.FileMode(0x600), true, true, true)

	if err != nil {
		t.Fatalf("FindOrCreate() failed, err: %v", err)
	}
	if got := r.FindByID(set.obj.ID); got.obj.ID != set.obj.ID {
		t.Fatalf("FindById(%d) failed, got: %+v, expected: %+v", set.obj.ID, got, set)
	}

	ops := []linux.Sembuf{
		{SemOp: -1},
	}
	chs := make([]chan struct{}, 0, 5)
	for i := 0; i < 5; i++ {
		ch := executeOps(ctx, t, set, ops, true)
		chs = append(chs, ch)
	}

	creds := auth.CredentialsFromContext(ctx)
	if err := r.Remove(set.obj.ID, creds); err != nil {
		t.Fatalf("Remove(%d) failed, err: %v", set.obj.ID, err)
	}
	set.mu.Lock()
	dead := set.dead
	set.mu.Unlock()
	if !dead {
		t.Fatalf("set is not dead: %+v", set)
	}
	if got := r.FindByID(set.obj.ID); got != nil {
		t.Fatalf("FindById(%d) failed, got: %+v, expected: nil", set.obj.ID, got)
	}
	for i, ch := range chs {
		if !signalled(ch) {
			t.Fatalf("channel %d should have been signalled", i)
		}
	}
}

// The callback deliberately violates countWaiters' contract. Both production
// predicates in its original callers only inspect w.value.
func TestWaiterCursorAcrossUnlock(t *testing.T) {
	ctx := contexttest.Context(t)
	creds := auth.CredentialsFromContext(ctx)
	r := NewRegistry(auth.NewRootUserNamespace())
	s, err := r.FindOrCreate(ctx, 123, 1, linux.FileMode(0o600), true, true, true)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		executeOps(ctx, t, s, []linux.Sembuf{{SemOp: -1}}, true)
	}
	checkCounts := func(wantZero, wantNegative uint16) {
		t.Helper()
		zero, err := s.CountZeroWaiters(0, creds)
		if err != nil || zero != wantZero {
			t.Fatalf("CountZeroWaiters = %d, %v; want %d, nil", zero, err, wantZero)
		}
		negative, err := s.CountNegativeWaiters(0, creds)
		if err != nil || negative != wantNegative {
			t.Fatalf("CountNegativeWaiters = %d, %v; want %d, nil", negative, err, wantNegative)
		}
	}
	checkCounts(0, 3)
	count, err := s.countWaiters(0, creds, func(w *waiter) bool {
		s.mu.Unlock()
		s.AbortWait(0, w.ch)
		s.mu.Lock()
		return true
	})
	if count != 1 || err != nil {
		t.Fatalf("count through stale cursor = %d, %v; want 1, nil", count, err)
	}
	checkCounts(0, 2)
	t.Log("queued before=3, visited=1, queued after=2")

	s.mu.Lock()
	before := s.findSem(0)
	s.mu.Unlock()
	if err := s.SetVal(ctx, 0, 3, creds, 17); err != nil {
		t.Fatal(err)
	}
	s.mu.Lock()
	same := s.findSem(0) == before
	val, pid := before.value, before.pid
	s.mu.Unlock()
	if !same || val != 3 || pid != 17 {
		t.Fatalf("same member=%t, value=%d, pid=%d; want true, 3, 17", same, val, pid)
	}
	checkCounts(0, 0)
	executeOps(ctx, t, s, []linux.Sembuf{{SemOp: 0}}, true)
	checkCounts(1, 0)
	if _, err := s.CountZeroWaiters(1, creds); err != linuxerr.ERANGE {
		t.Errorf("CountZeroWaiters for invalid slot = %v, want ERANGE", err)
	}
	t.Log("member stayed at the same address; after reacquiring the mutex it contained the updated value and PID")
}
