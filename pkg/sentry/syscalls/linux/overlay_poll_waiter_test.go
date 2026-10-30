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

package linux

import (
	"fmt"
	"testing"
	"time"
	"unsafe"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/overlay"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/proc"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/testutil"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/tmpfs"
	"gvisor.dev/gvisor/pkg/sentry/kernel"
	"gvisor.dev/gvisor/pkg/sentry/kernel/auth"
	"gvisor.dev/gvisor/pkg/sentry/vfs"
	"gvisor.dev/gvisor/pkg/waiter"
)

// This fixture runs the registration and cleanup functions called by pollBlock.
// A second poll stays registered on the same hostname notifier throughout.
func TestPollWaiterAfterOverlayCopyUp(t *testing.T) {
	k, err := testutil.Boot()
	if err != nil {
		t.Fatal(err)
	}
	ctx := k.SupervisorContext()
	creds := auth.CredentialsFromContext(ctx)
	fs := k.VFS()
	fs.MustRegisterFilesystemType(proc.Name, &proc.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	fs.MustRegisterFilesystemType(overlay.Name, overlay.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	ns, err := fs.NewMountNamespace(ctx, creds, "", tmpfs.Name, &vfs.MountOptions{}, k)
	if err != nil {
		t.Fatal(err)
	}
	s := testutil.NewSystem(ctx, t, fs, ns)
	defer s.Destroy()
	for _, path := range []string{"/proc", "/upper"} {
		if err := fs.MkdirAt(ctx, creds, s.PathOpAtRoot(path), &vfs.MkdirOptions{Mode: 0755}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := fs.MountAt(ctx, creds, "", s.PathOpAtRoot("/proc"), proc.Name, &vfs.MountOptions{GetFilesystemOptions: vfs.GetFilesystemOptions{InternalData: &proc.InternalData{}}}); err != nil {
		t.Fatal(err)
	}
	lower := s.GetDentryOrDie(s.PathOpAtRoot("/proc/sys/kernel"))
	defer lower.DecRef(ctx)
	upper := s.GetDentryOrDie(s.PathOpAtRoot("/upper"))
	defer upper.DecRef(ctx)
	ol, err := fs.NewMountNamespace(ctx, creds, "", overlay.Name, &vfs.MountOptions{GetFilesystemOptions: vfs.GetFilesystemOptions{InternalData: overlay.FilesystemOptions{UpperRoot: upper, LowerRoots: []vfs.VirtualDentry{lower}}}}, k)
	if err != nil {
		t.Fatal(err)
	}
	defer ol.DecRef(ctx)
	root := ol.Root(ctx)
	defer root.DecRef(ctx)
	path := s.PathOpAtRoot("hostname")
	path.Root, path.Start = root, root
	ofd, err := fs.OpenAt(ctx, creds, path, &vfs.OpenOptions{Flags: linux.O_RDONLY})
	if err != nil {
		t.Fatal(err)
	}
	defer ofd.DecRef(ctx)
	peer, err := fs.OpenAt(ctx, creds, s.PathOpAtRoot("/proc/sys/kernel/hostname"), &vfs.OpenOptions{Flags: linux.O_RDONLY})
	if err != nil {
		t.Fatal(err)
	}
	defer peer.DecRef(ctx)
	tg := k.NewThreadGroup(k.RootPIDNamespace(), kernel.NewSignalHandlers(), linux.SIGCHLD, k.GlobalInit().Limits())
	task, err := testutil.CreateTask(ctx, "overlay-poll", tg, s.MntNs, s.Root, s.Root)
	if err != nil {
		t.Fatal(err)
	}
	register := func(fd *vfs.FileDescription) ([]pollState, chan struct{}) {
		n, err := task.NewFDFrom(0, fd, kernel.FDFlags{})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { task.FDTable().Remove(task, n).DecRef(task) })
		state := make([]pollState, 1)
		ch := make(chan struct{}, 1)
		pfd := linux.PollFD{FD: n, Events: linux.POLLPRI | linux.POLLERR | linux.POLLHUP}
		if err := initReadiness(task, &pfd, &state[0], ch); err != nil {
			t.Fatal(err)
		}
		return state, ch
	}
	peerState, peerCh := register(peer)
	defer releaseState(task, peerState)
	for _, copyUp := range []bool{false, true} {
		state, ch := register(ofd)
		if copyUp {
			if err := ofd.SetStat(ctx, vfs.SetStatOptions{Stat: linux.Statx{Mask: linux.STATX_MODE, Mode: 0600}}); err != nil {
				t.Fatal(err)
			}
			// Force the open overlay description to adopt its copied-up backing.
			ofd.Readiness(waiter.EventPri)
		}
		releaseState(task, state)
		k.HostNamePoller.Notify()
		select {
		case <-peerCh:
		default:
			t.Fatal("active direct-proc poll failed to wake")
		}
		select {
		case <-ch:
			t.Errorf("released poll waiter received a hostname notification (copyUp=%t)", copyUp)
		default:
			t.Logf("released waiter stayed silent and active peer woke (copyUp=%t)", copyUp)
		}
	}
}

func TestOverlayPollCost(t *testing.T) {
	t.Logf("SIZES file=%d pollState=%d waiter=%d", unsafe.Sizeof(vfs.FileDescription{}), unsafe.Sizeof(pollState{}), unsafe.Sizeof(waiter.Entry{}))
	k, err := testutil.Boot()
	if err != nil {
		t.Fatal(err)
	}
	ctx := k.SupervisorContext()
	creds := auth.CredentialsFromContext(ctx)
	fs := k.VFS()
	fs.MustRegisterFilesystemType(proc.Name, &proc.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	fs.MustRegisterFilesystemType(overlay.Name, overlay.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	ns, err := fs.NewMountNamespace(ctx, creds, "", tmpfs.Name, &vfs.MountOptions{}, k)
	if err != nil {
		t.Fatal(err)
	}
	s := testutil.NewSystem(ctx, t, fs, ns)
	defer s.Destroy()
	for _, path := range []string{"/proc", "/upper", "/plain"} {
		if err := fs.MkdirAt(ctx, creds, s.PathOpAtRoot(path), &vfs.MkdirOptions{Mode: 0755}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := fs.MountAt(ctx, creds, "", s.PathOpAtRoot("/proc"), proc.Name, &vfs.MountOptions{GetFilesystemOptions: vfs.GetFilesystemOptions{InternalData: &proc.InternalData{}}}); err != nil {
		t.Fatal(err)
	}
	plain, err := fs.OpenAt(ctx, creds, s.PathOpAtRoot("/plain/hostname"), &vfs.OpenOptions{Flags: linux.O_CREAT | linux.O_RDWR, Mode: 0644})
	if err != nil {
		t.Fatal(err)
	}
	plain.DecRef(ctx)
	for _, spec := range []struct{ name, path string }{{"tmpfs", "/plain"}, {"proc", "/proc/sys/kernel"}} {
		t.Run(spec.name, func(t *testing.T) {
			lower := s.GetDentryOrDie(s.PathOpAtRoot(spec.path))
			defer lower.DecRef(ctx)
			upper := s.GetDentryOrDie(s.PathOpAtRoot("/upper"))
			defer upper.DecRef(ctx)
			ol, err := fs.NewMountNamespace(ctx, creds, "", overlay.Name, &vfs.MountOptions{GetFilesystemOptions: vfs.GetFilesystemOptions{InternalData: overlay.FilesystemOptions{UpperRoot: upper, LowerRoots: []vfs.VirtualDentry{lower}}}}, k)
			if err != nil {
				t.Fatal(err)
			}
			defer ol.DecRef(ctx)
			root := ol.Root(ctx)
			defer root.DecRef(ctx)
			path := s.PathOpAtRoot("hostname")
			path.Root, path.Start = root, root
			ofd, err := fs.OpenAt(ctx, creds, path, &vfs.OpenOptions{Flags: linux.O_RDONLY})
			if err != nil {
				t.Fatal(err)
			}
			defer ofd.DecRef(ctx)

			directFD, err := fs.OpenAt(ctx, creds, s.PathOpAtRoot(spec.path+"/hostname"), &vfs.OpenOptions{Flags: linux.O_RDONLY})
			if err != nil {
				t.Fatal(err)
			}
			defer directFD.DecRef(ctx)
			for _, tested := range []struct {
				name string
				fd   *vfs.FileDescription
			}{{spec.name, ofd}, {"direct-" + spec.name, directFD}} {
				for _, n := range []int{1, 8} {
					t.Run(fmt.Sprintf("%s/live%d", tested.name, n), func(t *testing.T) {
						es := make([]waiter.Entry, n)
						for i := range es {
							es[i].Init(waiter.NoopListener{}, waiter.EventPri)
						}
						cycle := func() {
							for j := range es {
								if err := tested.fd.EventRegister(&es[j]); err != nil {
									t.Fatal(err)
								}
							}
							for j := range es {
								tested.fd.EventUnregister(&es[j])
							}
						}
						for i := 0; i < 100; i++ {
							cycle()
						}
						allocs := testing.AllocsPerRun(1000, cycle)
						const repeats = 100000
						for i := 0; i < 10; i++ {
							started := time.Now()
							for j := 0; j < repeats; j++ {
								cycle()
							}
							ns := float64(time.Since(started).Nanoseconds()) / repeats
							fmt.Printf("BenchmarkOverlayPoll/%s/live%d %d %.2f ns/op %.2f allocs/op\n", tested.name, n, repeats, ns, allocs)
						}
					})
				}
			}
		})
	}
}

func TestOverlayLowerPollLifetime(t *testing.T) {
	k, err := testutil.Boot()
	if err != nil {
		t.Fatal(err)
	}
	ctx := k.SupervisorContext()
	creds := auth.CredentialsFromContext(ctx)
	fs := k.VFS()
	fs.MustRegisterFilesystemType(proc.Name, &proc.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	fs.MustRegisterFilesystemType(overlay.Name, overlay.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	ns, err := fs.NewMountNamespace(ctx, creds, "", tmpfs.Name, &vfs.MountOptions{}, k)
	if err != nil {
		t.Fatal(err)
	}
	s := testutil.NewSystem(ctx, t, fs, ns)
	defer s.Destroy()
	for _, path := range []string{"/proc", "/upper"} {
		if err := fs.MkdirAt(ctx, creds, s.PathOpAtRoot(path), &vfs.MkdirOptions{Mode: 0755}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := fs.MountAt(ctx, creds, "", s.PathOpAtRoot("/proc"), proc.Name, &vfs.MountOptions{GetFilesystemOptions: vfs.GetFilesystemOptions{InternalData: &proc.InternalData{}}}); err != nil {
		t.Fatal(err)
	}
	lower := s.GetDentryOrDie(s.PathOpAtRoot("/proc/sys/kernel"))
	defer lower.DecRef(ctx)
	upper := s.GetDentryOrDie(s.PathOpAtRoot("/upper"))
	defer upper.DecRef(ctx)
	ol, err := fs.NewMountNamespace(ctx, creds, "", overlay.Name, &vfs.MountOptions{GetFilesystemOptions: vfs.GetFilesystemOptions{InternalData: overlay.FilesystemOptions{UpperRoot: upper, LowerRoots: []vfs.VirtualDentry{lower}}}}, k)
	if err != nil {
		t.Fatal(err)
	}
	defer ol.DecRef(ctx)
	root := ol.Root(ctx)
	defer root.DecRef(ctx)
	path := s.PathOpAtRoot("hostname")
	path.Root, path.Start = root, root
	ofd, err := fs.OpenAt(ctx, creds, path, &vfs.OpenOptions{Flags: linux.O_RDONLY})
	if err != nil {
		t.Fatal(err)
	}
	defer ofd.DecRef(ctx)
	peer, err := fs.OpenAt(ctx, creds, s.PathOpAtRoot("/proc/sys/kernel/hostname"), &vfs.OpenOptions{Flags: linux.O_RDONLY})
	if err != nil {
		t.Fatal(err)
	}
	defer peer.DecRef(ctx)
	tg := k.NewThreadGroup(k.RootPIDNamespace(), kernel.NewSignalHandlers(), linux.SIGCHLD, k.GlobalInit().Limits())
	task, err := testutil.CreateTask(ctx, "overlay-poll", tg, s.MntNs, s.Root, s.Root)
	if err != nil {
		t.Fatal(err)
	}
	register := func(fd *vfs.FileDescription) ([]pollState, chan struct{}) {
		n, err := task.NewFDFrom(0, fd, kernel.FDFlags{})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { task.FDTable().Remove(task, n).DecRef(task) })
		state := make([]pollState, 1)
		ch := make(chan struct{}, 1)
		pfd := linux.PollFD{FD: n, Events: linux.POLLPRI | linux.POLLERR | linux.POLLHUP}
		if err := initReadiness(task, &pfd, &state[0], ch); err != nil {
			t.Fatal(err)
		}
		return state, ch
	}
	original := overlay.PollLowerForQualification(ofd)
	before := original.ReadRefs()
	first, firstCh := register(ofd)
	second, secondCh := register(ofd)
	if err := ofd.SetStat(ctx, vfs.SetStatOptions{Stat: linux.Statx{Mask: linux.STATX_MODE, Mode: 0600}}); err != nil {
		t.Fatal(err)
	}
	ofd.Readiness(waiter.EventPri)
	if got := original.ReadRefs(); got < before {
		t.Errorf("old lower no longer alive with two registrations: got %d, original %d", got, before)
	}
	upperState, upCh := register(ofd)
	k.HostNamePoller.Notify()
	for _, ch := range []chan struct{}{firstCh, secondCh} {
		select {
		case <-ch:
		default:
			t.Fatal("missing live lower notification")
		}
	}
	select {
	case <-upCh:
		t.Error("upper registration got lower notification")
	default:
	}
	releaseState(task, first)
	if got := original.ReadRefs(); got < before {
		t.Errorf("original lower lost with one waiter left: refs %d, want >=%d", got, before)
	}
	releaseState(task, upperState)
	k.HostNamePoller.Notify()
	select {
	case <-secondCh:
	default:
		t.Error("last lower waiter was detached while deleting upper")
	}
	select {
	case <-firstCh:
		t.Error("removed first waiter still notified")
	default:
	}
	releaseState(task, second)
	if got := original.ReadRefs(); got != before-1 {
		t.Errorf("old lower refs after both unregister: got %d, want %d", got, before-1)
	}
	t.Logf("initial lower refs=%d; final=%d", before, original.ReadRefs())
}

func TestStackedPollBinding(t *testing.T) {
	for _, innerFirst := range []bool{true, false} {
		t.Run(fmt.Sprintf("innerFirst=%t", innerFirst), func(t *testing.T) {
			k, err := testutil.Boot()
			if err != nil {
				t.Fatal(err)
			}
			ctx := k.SupervisorContext()
			creds := auth.CredentialsFromContext(ctx)
			fs := k.VFS()
			fs.MustRegisterFilesystemType(proc.Name, &proc.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
			fs.MustRegisterFilesystemType(overlay.Name, overlay.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
			ns, err := fs.NewMountNamespace(ctx, creds, "", tmpfs.Name, &vfs.MountOptions{}, k)
			if err != nil {
				t.Fatal(err)
			}
			s := testutil.NewSystem(ctx, t, fs, ns)
			defer s.Destroy()
			for _, p := range []string{"/proc", "/inner", "/outer"} {
				if err := fs.MkdirAt(ctx, creds, s.PathOpAtRoot(p), &vfs.MkdirOptions{Mode: 0755}); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := fs.MountAt(ctx, creds, "", s.PathOpAtRoot("/proc"), proc.Name, &vfs.MountOptions{GetFilesystemOptions: vfs.GetFilesystemOptions{InternalData: &proc.InternalData{}}}); err != nil {
				t.Fatal(err)
			}
			innerUpper := s.GetDentryOrDie(s.PathOpAtRoot("/inner"))
			defer innerUpper.DecRef(ctx)
			outerUpper := s.GetDentryOrDie(s.PathOpAtRoot("/outer"))
			defer outerUpper.DecRef(ctx)
			lower := s.GetDentryOrDie(s.PathOpAtRoot("/proc/sys/kernel"))
			defer lower.DecRef(ctx)
			mount := func(upper, lower vfs.VirtualDentry) *vfs.MountNamespace {
				ns, err := fs.NewMountNamespace(ctx, creds, "", overlay.Name, &vfs.MountOptions{GetFilesystemOptions: vfs.GetFilesystemOptions{InternalData: overlay.FilesystemOptions{UpperRoot: upper, LowerRoots: []vfs.VirtualDentry{lower}}}}, k)
				if err != nil {
					t.Fatal(err)
				}
				return ns
			}
			inner := mount(innerUpper, lower)
			defer inner.DecRef(ctx)
			innerRoot := inner.Root(ctx)
			defer innerRoot.DecRef(ctx)
			outer := mount(outerUpper, innerRoot)
			defer outer.DecRef(ctx)
			outerRoot := outer.Root(ctx)
			defer outerRoot.DecRef(ctx)
			path := s.PathOpAtRoot("hostname")
			path.Root, path.Start = outerRoot, outerRoot
			outerFD, err := fs.OpenAt(ctx, creds, path, &vfs.OpenOptions{Flags: linux.O_RDONLY})
			if err != nil {
				t.Fatal(err)
			}
			defer outerFD.DecRef(ctx)
			innerFD := overlay.PollLowerForQualification(outerFD)
			innerFD.IncRef()
			defer innerFD.DecRef(ctx)
			physical := overlay.PollLowerForQualification(innerFD)
			before := physical.ReadRefs()
			competing, ch := waiter.NewChannelEntry(waiter.EventPri | waiter.EventErr)
			stop, stopped := make(chan struct{}), make(chan struct{})
			go func() {
				defer close(stopped)
				for {
					select {
					case <-stop:
						return
					default:
						k.HostNamePoller.Notify()
					}
				}
			}()
			for i := 0; i < 128; i++ {
				if err := outerFD.EventRegister(&competing); err != nil {
					close(stop)
					<-stopped
					t.Fatal(err)
				}
				k.HostNamePoller.Notify()
				select {
				case <-ch:
				default:
					t.Error("registered callback failed to fire")
				}
				outerFD.EventUnregister(&competing)
			}
			close(stop)
			<-stopped
			if physical.ReadRefs() != before {
				t.Fatalf("competing registrations retained lower: got %d want %d", physical.ReadRefs(), before)
			}
			t.Log("128 registrations reused their entry under concurrent notifications")

			first, firstCh := waiter.NewChannelEntry(waiter.EventPri | waiter.EventErr)
			if err := outerFD.EventRegister(&first); err != nil {
				t.Fatal(err)
			}
			second, secondCh := waiter.NewChannelEntry(waiter.EventPri | waiter.EventErr)
			if err := outerFD.EventRegister(&second); err != nil {
				t.Fatal(err)
			}
			if refs := physical.ReadRefs(); refs != before+2 {
				t.Fatalf("two forwarded registrations: refs=%d want %d", refs, before+2)
			}
			promote := func(fd *vfs.FileDescription) {
				if err := fd.SetStat(ctx, vfs.SetStatOptions{Stat: linux.Statx{Mask: linux.STATX_MODE, Mode: 0600}}); err != nil {
					t.Fatal(err)
				}
				fd.Readiness(waiter.EventPri)
			}
			if innerFirst {
				promote(innerFD)
				promote(outerFD)
			} else {
				promote(outerFD)
				promote(innerFD)
			}
			if physical.ReadRefs() != before+1 {
				t.Fatalf("copied-up references: got %d want %d", physical.ReadRefs(), before+1)
			}
			outerFD.EventUnregister(&first)
			if _, ok := first.Listener().(waiter.ChannelNotifier); !ok {
				t.Fatal("caller listener not restored")
			}
			if err := outerFD.EventRegister(&first); err != nil {
				t.Fatal(err)
			}
			k.HostNamePoller.Notify()
			select {
			case <-firstCh:
				t.Error("removed waiter woke")
			default:
			}
			select {
			case <-secondCh:
			default:
				t.Fatal("remaining waiter lost")
			}
			outerFD.EventUnregister(&first)
			outerFD.EventUnregister(&second)
			if refs := physical.ReadRefs(); refs != before-1 {
				t.Fatalf("final references=%d want %d", refs, before-1)
			}
			t.Logf("innerFirst=%t selected final proc file, removed waiter silent, second woke, final refs=%d", innerFirst, physical.ReadRefs())
		})
	}
}
