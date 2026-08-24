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

package kernel

import (
	"sync"
	"testing"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/errors/linuxerr"
	"gvisor.dev/gvisor/pkg/sentry/arch"
	"gvisor.dev/gvisor/pkg/sentry/kernel/auth"
	"gvisor.dev/gvisor/pkg/sentry/kernel/sched"
	"gvisor.dev/gvisor/pkg/sentry/mm"
	"gvisor.dev/gvisor/pkg/sentry/platform"
)

func TestParentDeathSignalClearedOnCredentialChange(t *testing.T) {
	for _, test := range []struct {
		name   string
		change func(*Task)
	}{
		{
			name: "uid",
			change: func(task *Task) {
				task.setKUIDsUnchecked(0, 1, 0)
			},
		},
		{
			name: "gid",
			change: func(task *Task) {
				task.setKGIDsUnchecked(0, 1, 0)
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			// These helpers only use the MemoryManager's atomic dumpability metadata;
			// this test does not need a live address space or platform.
			task := &Task{
				image: TaskImage{MemoryManager: new(mm.MemoryManager)},
			}
			task.creds.Store(auth.NewRootCredentials(auth.NewRootUserNamespace()))
			task.SetParentDeathSignal(linux.SIGUSR1)

			var wg sync.WaitGroup
			wg.Go(func() { test.change(task) })
			// Read before Wait so the reset and read are not ordered by the
			// wait group. Under -race, this checks that the reset takes Task.mu.
			observed := task.ParentDeathSignal()
			wg.Wait()
			if observed != 0 && observed != linux.SIGUSR1 {
				t.Errorf("concurrent ParentDeathSignal() = %d, want 0 or %d", observed, linux.SIGUSR1)
			}
			if got := task.ParentDeathSignal(); got != 0 {
				t.Errorf("ParentDeathSignal() after credential change = %d, want 0", got)
			}
		})
	}
}

func TestTaskCPU(t *testing.T) {
	for _, test := range []struct {
		mask sched.CPUSet
		tid  ThreadID
		cpu  int32
	}{
		{
			mask: []byte{0xff},
			tid:  1,
			cpu:  1,
		},
		{
			mask: []byte{0xff},
			tid:  10,
			cpu:  2,
		},
		{
			// more than 8 cpus.
			mask: []byte{0xff, 0xff},
			tid:  10,
			cpu:  10,
		},
		{
			// missing the first cpu.
			mask: []byte{0xfe},
			tid:  1,
			cpu:  2,
		},
		{
			mask: []byte{0xfe},
			tid:  10,
			cpu:  4,
		},
		{
			// missing the fifth cpu.
			mask: []byte{0xef},
			tid:  10,
			cpu:  3,
		},
		{
			// only the fifth cpu.
			mask: []byte{0x10},
			tid:  10,
			cpu:  4,
		},
	} {
		assigned := assignCPU(test.mask, test.tid)
		if test.cpu != assigned {
			t.Errorf("assignCPU(%v, %v) got %v, want %v", test.mask, test.tid, assigned, test.cpu)
		}
	}

}

func TestExitNotifyParentSeccheckInfoConcurrentGroupExit(t *testing.T) {
	// kernel_test configures no optional seccheck context fields, so the
	// reader needs only these ownership links and the group's signal state.
	k := &Kernel{tasks: newTaskSet(&PIDNamespace{})}
	tg := &ThreadGroup{
		threadGroupNode: threadGroupNode{pidns: k.tasks.Root},
		signalHandlers:  NewSignalHandlers(),
	}
	task := &Task{
		taskNode: taskNode{tg: tg},
		k:        k,
	}
	tg.leader = task
	tg.tasks.PushBack(task)
	wantStatus := linux.WaitStatusTerminationSignal(linux.SIGKILL)

	task.k.tasks.mu.RLock()
	defer task.k.tasks.mu.RUnlock()
	var wg sync.WaitGroup
	wg.Go(func() { task.PrepareGroupExit(wantStatus) })
	// Read before Wait so the wait group does not order the status accesses.
	_, info := getExitNotifyParentSeccheckInfo(task)
	wg.Wait()
	if got := info.ExitStatus; got != 0 && got != int32(wantStatus) {
		t.Errorf("concurrent exit status = %d, want 0 or %d", got, wantStatus)
	}
	_, info = getExitNotifyParentSeccheckInfo(task)
	if got := info.ExitStatus; got != int32(wantStatus) {
		t.Errorf("exit status after group exit = %d, want %d", got, wantStatus)
	}
}

type pullFullStateErrorContext struct {
	platform.Context
}

func (*pullFullStateErrorContext) PullFullState(platform.AddressSpace, *arch.Context64) error {
	return linuxerr.EIO
}

type pullFullStateErrorPlatform struct {
	platform.Platform
}

func (*pullFullStateErrorPlatform) SupportsAddressSpaceIO() bool { return false }

func (*pullFullStateErrorPlatform) NewAddressSpace() (platform.AddressSpace, error) {
	return &pullFullStateErrorAddressSpace{}, nil
}

type pullFullStateErrorAddressSpace struct {
	platform.AddressSpace
}

func (*pullFullStateErrorAddressSpace) Release() {}

func TestRunInterruptPullFullStateError(t *testing.T) {
	// PullFullState returns an error without accessing application memory.
	// Construct a valid, empty MemoryManager without backing memory or a live
	// platform address space. Unexpected platform operations fail through the
	// nil embedded interfaces above.
	memoryManager, err := mm.NewMemoryManager(&pullFullStateErrorPlatform{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer memoryManager.DecUsers(context.Background())

	tg := &ThreadGroup{signalHandlers: NewSignalHandlers()}
	task := &Task{
		taskNode: taskNode{tg: tg},
		image: TaskImage{
			Arch:          new(arch.Context64),
			MemoryManager: memoryManager,
		},
		p: &pullFullStateErrorContext{},
	}
	tg.tasks.PushBack(task)
	tg.signalHandlers.mu.Lock()
	queued := task.pendingSignals.enqueue(SignalInfoPriv(linux.SIGUSR1), nil)
	tg.signalHandlers.mu.Unlock()
	if !queued {
		t.Fatal("failed to enqueue signal")
	}

	if next := (*runInterrupt)(nil).execute(task); next != (*runExit)(nil) {
		t.Fatalf("runInterrupt returned %T, want *runExit", next)
	}

	// The error path must release the signal mutex before entering runExit.
	tg.signalHandlers.mu.Lock()
	defer tg.signalHandlers.mu.Unlock()
	if !tg.exiting {
		t.Error("thread group is not exiting")
	}
	wantStatus := linux.WaitStatusTerminationSignal(linux.SIGILL)
	if got := tg.exitStatus; got != wantStatus {
		t.Errorf("thread group exit status = %v, want %v", got, wantStatus)
	}
	if got := task.exitStatus; got != wantStatus {
		t.Errorf("task exit status = %v, want %v", got, wantStatus)
	}
}

func TestCreateSessionSharedSignalHandlers(t *testing.T) {
	ctx := context.Background()
	pidns := newPIDNamespace(nil, nil, nil)
	ts := newTaskSet(pidns)
	k := &Kernel{tasks: ts}
	var leaders []*Task
	defer func() {
		ts.mu.Lock()
		defer ts.mu.Unlock()
		for i := len(leaders) - 1; i >= 0; i-- {
			leader := leaders[i]
			delete(pidns.tgids, leader.tg)
			delete(pidns.tasks, pidns.tids[leader])
			delete(pidns.tids, leader)
			if pg := leader.tg.processGroup; pg != nil {
				// Every leader and process-group originator was created in pidns.
				// checklocks cannot connect these collection members to ts.mu.
				pg.decRefWithParent(leader.tg.parentPG()) // +checklocksignore
			}
		}
	}()

	// These operations need only thread and namespace metadata. No tasks
	// run or enter a group stop, so no platform context is needed.
	newTask := func(id, parentID ThreadID, sh *SignalHandlers) *Task {
		ts.mu.Lock()
		defer ts.mu.Unlock()
		parent := pidns.tasks[parentID]
		tg := &ThreadGroup{
			threadGroupNode: threadGroupNode{pidns: pidns},
			signalHandlers:  sh,
		}
		leader := &Task{k: k, taskNode: taskNode{
			tg:       tg,
			parent:   parent,
			children: make(map[*Task]struct{}),
		}}
		tg.leader = leader
		tg.tasks.PushBack(leader)
		pidns.tgids[tg] = id
		pidns.tids[leader] = id
		pidns.tasks[id] = leader
		if parent != nil {
			parent.children[leader] = struct{}{}
			tg.processGroup = parent.tg.processGroup
			// parent comes from pidns.tasks, so its process group belongs to ts.
			// checklocks cannot infer this map-member ownership.
			tg.processGroup.incRefWithParent(tg.processGroup) // +checklocksignore
		}
		leaders = append(leaders, leader)
		return leader
	}

	root := newTask(1, 0, NewSignalHandlers())
	if _, err := root.tg.CreateSession(ctx); err != nil {
		t.Fatal(err)
	}
	parent := newTask(2, 1, NewSignalHandlers())
	// This is the topology produced by CLONE_VM|CLONE_SIGHAND without
	// CLONE_THREAD or CLONE_PARENT.
	child := newTask(3, 2, parent.tg.signalHandlers)
	if err := child.tg.CreateProcessGroup(); err != nil {
		t.Fatal(err)
	}
	childPG := child.tg.ProcessGroup()
	if childPG.IsOrphan() {
		t.Fatal("child process group is already orphaned")
	}

	// Leaving the session orphans the child's process group. The orphan
	// check must not reacquire the shared signal mutex held for parent.
	if _, err := parent.tg.CreateSession(ctx); err != nil {
		t.Fatal(err)
	}
	if !childPG.IsOrphan() {
		t.Error("child process group was not orphaned")
	}
	if child.tg.Session() != root.tg.Session() {
		t.Error("child left the original session")
	}
	if parent.tg.Session() == root.tg.Session() {
		t.Error("parent did not create a new session")
	}
}

// testTTYOperations has no backend resources. SetControllingTTY only uses
// its reference operations; OpenTTY is intentionally unavailable.
type testTTYOperations struct {
	TTYOperations
}

func (testTTYOperations) IncRef() {}

func (testTTYOperations) DecRef(context.Context) {}

func TestSetControllingTTYSharedSignalHandlers(t *testing.T) {
	userns := auth.NewRootUserNamespace()
	ctx := auth.ContextWithCredentials(context.Background(), auth.NewRootCredentials(userns))
	pidns := newPIDNamespace(nil, nil, userns)
	ts := newTaskSet(pidns)
	sh := NewSignalHandlers()

	// Model the relevant state after a CLONE_VM|CLONE_SIGHAND child
	// calls setsid: two session leaders with the same signal handlers.
	newSessionLeader := func(id ThreadID) *ThreadGroup {
		tg := &ThreadGroup{
			threadGroupNode: threadGroupNode{pidns: pidns},
			signalHandlers:  sh,
		}
		tg.processGroup = &ProcessGroup{originator: tg, session: &Session{leader: tg}}
		ts.Root.tgids[tg] = id
		return tg
	}
	oldOwner := newSessionLeader(1)
	newOwner := newSessionLeader(2)
	tty := NewTTY(0, testTTYOperations{})
	if err := oldOwner.SetControllingTTY(ctx, tty, false /* steal */, true /* isReadable */); err != nil {
		t.Fatal(err)
	}

	if err := newOwner.SetControllingTTY(ctx, tty, true /* steal */, true /* isReadable */); err != nil {
		t.Fatal(err)
	}
	if got := oldOwner.TTY(); got != nil {
		t.Errorf("old owner's controlling TTY = %p, want nil", got)
	}
	if got := newOwner.TTY(); got != tty {
		t.Errorf("new owner's controlling TTY = %p, want %p", got, tty)
	}
	if got := tty.ThreadGroup(); got != newOwner {
		t.Errorf("TTY controller = %p, want %p", got, newOwner)
	}
}
