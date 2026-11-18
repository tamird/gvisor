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
	"bytes"
	"reflect"
	"slices"
	"testing"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/errors/linuxerr"
	"gvisor.dev/gvisor/pkg/sentry/contexttest"
	"gvisor.dev/gvisor/pkg/sentry/kernel/auth"
	"gvisor.dev/gvisor/pkg/sentry/kernel/ipc"
	"gvisor.dev/gvisor/pkg/state"
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
	set.initSemaphores()
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
	set.initSemaphores()
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
	set.initSemaphores()
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

func TestWaitMinInt16(t *testing.T) {
	ctx := contexttest.Context(t)
	set := &Set{obj: &ipc.Object{ID: 123}, sems: make([]sem, 1)}
	set.initSemaphores()
	ops := []linux.Sembuf{
		{SemOp: valueMax},
	}
	executeOps(ctx, t, set, ops, false)

	ops[0].SemOp = -32768
	ops[0].SemFlg = linux.IPC_NOWAIT
	set.mu.Lock()
	_, _, err := set.executeOps(ctx, ops, 123)
	value := set.findSem(0).value
	set.mu.Unlock()
	if err != linuxerr.ErrWouldBlock {
		t.Fatalf("ExecuteOps(ops) wrong result, got: %v, expected: %v", err, linuxerr.ErrWouldBlock)
	}
	if value != valueMax {
		t.Fatalf("semaphore value got: %d, expected: %d", value, valueMax)
	}

	ops[0].SemFlg = 0
	executeOps(ctx, t, set, ops, true)
}

func TestWaitMinInt16NotWoken(t *testing.T) {
	ctx := contexttest.Context(t)
	set := &Set{obj: &ipc.Object{ID: 123}, sems: make([]sem, 1)}
	set.initSemaphores()
	ch := executeOps(ctx, t, set, []linux.Sembuf{{SemOp: -32768}}, true)

	executeOps(ctx, t, set, []linux.Sembuf{{SemOp: 1}}, false)
	if signalled(ch) {
		t.Fatalf("ExecuteOps(ops) channel should not have been signalled")
	}
}

func TestUnregister(t *testing.T) {
	ctx := contexttest.Context(t)
	r := NewRegistry(auth.NewRootUserNamespace())
	set, err := r.FindOrCreate(ctx, 123, 2, linux.FileMode(0600), true, true, true)

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
	if got, err := set.CountNegativeWaiters(0, creds); err != nil || got != 0 {
		t.Fatalf("queued waiters after removal = (%d, %v), want (0, nil)", got, err)
	}
	for i, ch := range chs {
		if !signalled(ch) {
			t.Fatalf("channel %d should have been signalled", i)
		}
	}
}

// A semaphore is stable storage; releasing the mutex does not expire its handle.
func TestMemberAfterRelock(t *testing.T) {
	ctx := contexttest.Context(t)
	registry := NewRegistry(auth.NewRootUserNamespace())
	set, err := registry.FindOrCreate(ctx, 123, 2, linux.FileMode(0600), true, true, true)
	if err != nil {
		t.Fatal(err)
	}
	set.mu.Lock()
	member := set.findSem(1)
	member.value = 7
	member.pid = 123
	set.mu.Unlock()

	set.mu.Lock()
	defer set.mu.Unlock()
	if member != set.findSem(1) || member.mu != &set.mu {
		t.Fatal("semaphore handle lost its original owner")
	}
	member.wakeWaiters()
	if member.value != 7 || member.pid != 123 {
		t.Fatalf("member after relock: value=%d pid=%d", member.value, member.pid)
	}
}

func TestRestoreMutexLinks(t *testing.T) {
	original := &Set{sems: make([]sem, 2)}
	original.initSemaphores()
	original.mu.Lock()
	original.findSem(0).value = 7
	original.findSem(1).pid = 123
	original.mu.Unlock()

	if got, want := original.findSem(0).StateFields(), []string{"value", "pid"}; !slices.Equal(got, want) {
		t.Fatalf("saved semaphore fields = %v, want %v", got, want)
	}
	var data bytes.Buffer
	if _, err := state.Save(t.Context(), &data, &original); err != nil {
		t.Fatal(err)
	}
	t.Logf("sem size=%d; encoded state bytes=%d", reflect.TypeFor[sem]().Size(), data.Len())
	var restored *Set
	if _, err := state.Load(t.Context(), &data, &restored); err != nil {
		t.Fatal(err)
	}
	for i := range restored.sems {
		if restored.findSem(int32(i)).mu != &restored.mu {
			t.Fatalf("restored semaphore %d has the wrong mutex", i)
		}
	}
	restored.mu.Lock()
	defer restored.mu.Unlock()
	if restored.findSem(0).value != 7 || restored.findSem(1).pid != 123 {
		t.Fatal("restoring mutex links changed saved semaphore values")
	}
}
