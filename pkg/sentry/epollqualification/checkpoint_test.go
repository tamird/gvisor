package qualification

import (
	"bytes"
	"encoding/binary"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/sentry/contexttest"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/eventfd"
	"gvisor.dev/gvisor/pkg/sentry/vfs"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/waiter"
)

func TestModifiedEpollCheckpoint(t *testing.T) {
	t.Run("one_change", func(t *testing.T) { checkModifiedEpollCheckpoint(t, false) })
	t.Run("two_changes", func(t *testing.T) { checkModifiedEpollCheckpoint(t, true) })
}

func checkModifiedEpollCheckpoint(t *testing.T, changeTwice bool) {
	ctx := contexttest.Context(t)
	var fs vfs.VirtualFilesystem
	if err := fs.Init(ctx); err != nil {
		t.Fatal(err)
	}
	defer fs.Release(ctx)
	epfd, err := fs.NewEpollInstanceFD(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer epfd.DecRef(ctx)
	efd, err := eventfd.New(ctx, &fs, 0, false, linux.O_RDWR)
	if err != nil {
		t.Fatal(err)
	}
	defer efd.DecRef(ctx)
	ep := epfd.Impl().(*vfs.EpollInstance)
	if err := ep.AddInterest(efd, 3, linux.EpollEvent{Events: linux.EPOLLOUT | linux.EPOLLET}); err != nil {
		t.Fatal(err)
	}
	if got := ep.ReadEvents(nil, 1); len(got) != 1 || got[0].Events != linux.EPOLLOUT {
		t.Fatalf("initial event: %v", got)
	}
	watch := linux.EpollEvent{Events: linux.EPOLLIN | linux.EPOLLET, Data: [2]int32{17}}
	if changeTwice {
		intermediate := watch
		intermediate.Events |= linux.EPOLLPRI
		if err := ep.ModifyInterest(efd, 3, intermediate); err != nil {
			t.Fatal(err)
		}
	}
	if err := ep.ModifyInterest(efd, 3, watch); err != nil {
		t.Fatal(err)
	}
	snapshot := [2]*vfs.FileDescription{epfd, efd}
	var buf bytes.Buffer
	if _, err := state.Save(ctx, &buf, &snapshot); err != nil {
		t.Fatal(err)
	}
	var restored [2]*vfs.FileDescription
	if _, err := state.Load(ctx, &buf, &restored); err != nil {
		t.Fatal(err)
	}
	defer restored[0].VirtualDentry().Mount().Filesystem().VirtualFilesystem().Release(ctx)
	defer restored[1].DecRef(ctx)
	defer restored[0].DecRef(ctx)
	ep = restored[0].Impl().(*vfs.EpollInstance)
	// Draining the restore readiness recheck before signaling distinguishes
	// a genuinely live restored listener from a one-time repoll.
	if got := ep.ReadEvents(nil, 1); len(got) != 0 {
		t.Fatalf("readable before signal: %v", got)
	}
	if err := restored[1].Impl().(*eventfd.EventFileDescription).Signal(1); err != nil {
		t.Fatal(err)
	}
	want := linux.EpollEvent{Events: linux.EPOLLIN, Data: watch.Data}
	if got := ep.ReadEvents(nil, 1); len(got) != 1 || got[0] != want {
		t.Fatalf("restored event: got %v, want %v", got, want)
	}
	if got := ep.ReadEvents(nil, 1); len(got) != 0 {
		t.Fatalf("redelivered edge: %v", got)
	}
	watch.Events = linux.EPOLLOUT | linux.EPOLLET
	if err := ep.ModifyInterest(restored[1], 3, watch); err != nil {
		t.Fatal(err)
	}
	want.Events = linux.EPOLLOUT
	if got := ep.ReadEvents(nil, 1); len(got) != 1 || got[0] != want {
		t.Fatalf("MOD after restore: got %v, want %v", got, want)
	}
	if err := ep.DeleteInterest(restored[1], 3); err != nil {
		t.Fatal(err)
	}
}

func TestEpollModifyAllocations(t *testing.T) {
	ctx := contexttest.Context(t)
	var fs vfs.VirtualFilesystem
	if err := fs.Init(ctx); err != nil {
		t.Fatal(err)
	}
	defer fs.Release(ctx)
	epfd, err := fs.NewEpollInstanceFD(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer epfd.DecRef(ctx)
	efd, err := eventfd.New(ctx, &fs, 0, false, linux.O_RDWR)
	if err != nil {
		t.Fatal(err)
	}
	defer efd.DecRef(ctx)
	ep := epfd.Impl().(*vfs.EpollInstance)
	event := linux.EpollEvent{Events: linux.EPOLLIN}
	if err := ep.AddInterest(efd, 3, event); err != nil {
		t.Fatal(err)
	}
	for _, change := range []bool{false, true} {
		allocs := testing.AllocsPerRun(1000, func() {
			if change {
				event.Events ^= linux.EPOLLOUT
			}
			if err := ep.ModifyInterest(efd, 3, event); err != nil {
				t.Fatal(err)
			}
		})
		t.Logf("epoll MOD change-mask=%t allocations/call %.3f", change, allocs)
	}
}

func TestEpollEventfdPromotedToHost(t *testing.T) {
	ctx := contexttest.Context(t)
	var fs vfs.VirtualFilesystem
	if err := fs.Init(ctx); err != nil {
		t.Fatal(err)
	}
	defer fs.Release(ctx)
	epfd, err := fs.NewEpollInstanceFD(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer epfd.DecRef(ctx)
	efd, err := eventfd.New(ctx, &fs, 0, false, linux.O_RDWR)
	if err != nil {
		t.Fatal(err)
	}
	defer efd.DecRef(ctx)
	ep := epfd.Impl().(*vfs.EpollInstance)
	if err := ep.AddInterest(efd, 3, linux.EpollEvent{Events: linux.EPOLLOUT | linux.EPOLLET}); err != nil {
		t.Fatal(err)
	}
	if got := ep.ReadEvents(nil, 1); len(got) != 1 || got[0].Events != linux.EPOLLOUT {
		t.Fatalf("initial event: got %v, want EPOLLOUT", got)
	}
	host, err := efd.Impl().(*eventfd.EventFileDescription).HostFD()
	if err != nil {
		t.Fatal(err)
	}
	watch := linux.EpollEvent{Events: linux.EPOLLIN | linux.EPOLLET, Data: [2]int32{17}}
	if err := ep.ModifyInterest(efd, 3, watch); err != nil {
		t.Fatal(err)
	}
	if got := ep.ReadEvents(nil, 1); len(got) != 0 {
		t.Fatalf("readable before host write: %v", got)
	}
	w, ch := waiter.NewChannelEntry(waiter.ReadableEvents)
	if err := epfd.EventRegister(&w); err != nil {
		t.Fatal(err)
	}
	defer epfd.EventUnregister(&w)
	var buf [8]byte
	binary.NativeEndian.PutUint64(buf[:], 1)
	if _, err := unix.Write(host, buf[:]); err != nil {
		t.Fatal(err)
	}
	select {
	case <-ch:
	case <-time.After(5 * time.Second):
		t.Fatal("host eventfd did not wake the installed interest")
	}
	want := linux.EpollEvent{Events: linux.EPOLLIN, Data: watch.Data}
	if got := ep.ReadEvents(nil, 1); len(got) != 1 || got[0] != want {
		t.Fatalf("host event: got %v, want %v", got, want)
	}
}
