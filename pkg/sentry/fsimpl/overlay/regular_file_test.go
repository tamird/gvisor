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

package overlay

import (
	"testing"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/errors/linuxerr"
	"gvisor.dev/gvisor/pkg/fspath"
	"gvisor.dev/gvisor/pkg/sentry/contexttest"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/tmpfs"
	"gvisor.dev/gvisor/pkg/sentry/kernel/auth"
	"gvisor.dev/gvisor/pkg/sentry/vfs"
)

// blockingCloseFD delegates to the lower file except for its close callback.
type blockingCloseFD struct {
	vfs.FileDescriptionImpl
	lower                     *vfs.FileDescription
	entered, resume, released chan struct{}
}

func (fd *blockingCloseFD) OnClose(context.Context) error {
	close(fd.entered)
	<-fd.resume
	return linuxerr.EIO
}

func (fd *blockingCloseFD) Release(ctx context.Context) {
	fd.lower.DecRef(ctx)
	close(fd.released)
}

func TestOnCloseDuringCopyUp(t *testing.T) {
	ctx := contexttest.Context(t)
	creds := auth.CredentialsFromContext(ctx)
	var v vfs.VirtualFilesystem
	if err := v.Init(ctx); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { v.Release(ctx) })
	v.MustRegisterFilesystemType("tmpfs", tmpfs.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	v.MustRegisterFilesystemType("overlay", FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	mount := func(name string, opts vfs.MountOptions) vfs.VirtualDentry {
		t.Helper()
		ns, err := v.NewMountNamespace(ctx, creds, "", name, &opts, nil)
		if err != nil {
			t.Fatalf("mounting %s: %v", name, err)
		}
		root := ns.Root(ctx)
		t.Cleanup(func() {
			root.DecRef(ctx)
			ns.DecRef(ctx)
		})
		return root
	}
	open := func(root vfs.VirtualDentry, flags uint32) *vfs.FileDescription {
		t.Helper()
		fd, err := v.OpenAt(ctx, creds, &vfs.PathOperation{
			Root: root, Start: root, Path: fspath.Parse("file"),
		}, &vfs.OpenOptions{Flags: flags, Mode: 0644})
		if err != nil {
			t.Fatalf("opening file: %v", err)
		}
		return fd
	}
	lower := mount("tmpfs", vfs.MountOptions{})
	open(lower, linux.O_CREAT|linux.O_RDWR).DecRef(ctx)
	upper := mount("tmpfs", vfs.MountOptions{})
	root := mount("overlay", vfs.MountOptions{GetFilesystemOptions: vfs.GetFilesystemOptions{
		InternalData: FilesystemOptions{UpperRoot: upper, LowerRoots: []vfs.VirtualDentry{lower}},
	}})
	fd := open(root, linux.O_RDONLY)
	defer fd.DecRef(ctx)

	// Make the lower close block, as it may while waiting for a FUSE server.
	reg := fd.Impl().(*regularFileFD)
	reg.mu.Lock()
	old := reg.cachedFD
	tracked := &blockingCloseFD{FileDescriptionImpl: old.Impl(), lower: old,
		entered: make(chan struct{}), resume: make(chan struct{}), released: make(chan struct{})}
	replacement := &vfs.FileDescription{}
	if err := replacement.Init(tracked, old.StatusFlags(), creds, old.Mount(), old.Dentry(), &vfs.FileDescriptionOptions{}); err != nil {
		reg.mu.Unlock()
		t.Fatal(err)
	}
	reg.cachedFD = replacement // transfer the cache's old reference to tracked
	reg.mu.Unlock()

	result := make(chan error, 1)
	go func() { result <- fd.OnClose(ctx) }()
	<-tracked.entered
	// A second user of the same open description changes the mode. This
	// copies the file up and replaces the cached lower descriptor.
	err := fd.SetStat(ctx, vfs.SetStatOptions{Stat: linux.Statx{Mask: linux.STATX_MODE, Mode: 0600}})
	if err != nil {
		t.Errorf("changing mode through overlay: %v", err)
	}
	select {
	case <-tracked.released:
		t.Error("lower file released while its OnClose was still running")
	default:
	}
	close(tracked.resume)
	if got, want := <-result, linuxerr.EIO; got != want {
		t.Errorf("OnClose: got %v, want %v", got, want)
	}
	select {
	case <-tracked.released:
	default:
		t.Error("lower file not released after its OnClose returned")
	}
}
