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

package overlay_test

import (
	"testing"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/overlay"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/proc"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/testutil"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/tmpfs"
	"gvisor.dev/gvisor/pkg/sentry/kernel/auth"
	"gvisor.dev/gvisor/pkg/sentry/vfs"
	"gvisor.dev/gvisor/pkg/waiter"
)

func TestUnregisterAfterCopyUp(t *testing.T) {
	k, err := testutil.Boot()
	if err != nil {
		t.Fatal(err)
	}
	ctx := k.SupervisorContext()
	creds := auth.CredentialsFromContext(ctx)
	fs := k.VFS()
	fs.MustRegisterFilesystemType(proc.Name, &proc.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	fs.MustRegisterFilesystemType(overlay.Name, overlay.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	mount := func(name string, data any) *testutil.System {
		t.Helper()
		ns, err := fs.NewMountNamespace(ctx, creds, "", name, &vfs.MountOptions{
			GetFilesystemOptions: vfs.GetFilesystemOptions{InternalData: data},
		}, k)
		if err != nil {
			t.Fatal(err)
		}
		s := testutil.NewSystem(ctx, t, fs, ns)
		t.Cleanup(s.Destroy)
		return s
	}
	// The lower hostname has a notifier. Its writable copy is a tmpfs file.
	lower := mount(proc.Name, &proc.InternalData{})
	lowerRoot := lower.GetDentryOrDie(lower.PathOpAtRoot("/sys/kernel"))
	defer lowerRoot.DecRef(ctx)
	upper := mount(tmpfs.Name, nil)
	merged := mount(overlay.Name, overlay.FilesystemOptions{
		LowerRoots: []vfs.VirtualDentry{lowerRoot}, UpperRoot: upper.Root,
	})
	fd, err := fs.OpenAt(ctx, creds, merged.PathOpAtRoot("/hostname"), &vfs.OpenOptions{Flags: linux.O_RDONLY})
	if err != nil {
		t.Fatal(err)
	}
	defer fd.DecRef(ctx)

	entry, ch := waiter.NewChannelEntry(waiter.EventPri)
	func() {
		if err := fd.EventRegister(&entry); err != nil {
			t.Fatal(err)
		}
		defer fd.EventUnregister(&entry)
		if err := fd.SetStat(ctx, vfs.SetStatOptions{Stat: linux.Statx{Mask: linux.STATX_MODE, Mode: 0600}}); err != nil {
			t.Fatal(err)
		}
		// A new registration makes the open FD switch to its upper backing.
		upperEntry, _ := waiter.NewChannelEntry(waiter.EventPri)
		if err := fd.EventRegister(&upperEntry); err != nil {
			t.Fatal(err)
		}
		fd.EventUnregister(&upperEntry)
		k.HostNamePoller.Notify()
		select {
		case <-ch:
		default:
			t.Fatal("lower waiter stopped receiving notifications before unregister")
		}
	}()

	control, controlCh := waiter.NewChannelEntry(waiter.EventPri)
	k.HostNamePoller.EventRegister(&control)
	defer k.HostNamePoller.EventUnregister(&control)
	k.HostNamePoller.Notify()
	select {
	case <-controlCh:
	default:
		t.Fatal("active hostname waiter did not receive the notification")
	}
	select {
	case <-ch:
		t.Error("removed overlay waiter received a hostname notification")
	default:
	}
}
