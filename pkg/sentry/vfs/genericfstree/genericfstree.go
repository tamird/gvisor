// Copyright 2020 The gVisor Authors.
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

// Package genericfstree provides tools for implementing vfs.FilesystemImpls
// that follow a standard pattern for synchronizing Dentry parent and name.
//
// TODO: As of this writing, every filesystem implementation with its own
// dentry type uses at least part of genericfstree, suggesting that we may want
// to merge its functionality into vfs.Dentry. However, this will incur the
// cost of an extra (entirely predictable) branch per directory traversal,
// since Dentry.parent will need to be atomic.Pointer[vfs.Dentry] rather than a
// filesystem-specific dentry, requiring a type assertion to the latter.
package genericfstree

import (
	"sync/atomic"

	"gvisor.dev/gvisor/pkg/fspath"
	"gvisor.dev/gvisor/pkg/sentry/vfs"
)

// RWMutex is a read/write mutex.
//
// TODO: tie this, fs, and dentry together.
type RWMutex interface {
	Lock()
	Unlock()
	RLock()
	RUnlock()
}

// Filesystem is implemented by filesystems that expose an ancestry mutex.
type Filesystem interface {
	// AncestryMu makes parent and name writes atomic for all dentries in the
	// filesystem.
	AncestryMu() RWMutex
}

// Dentry is implemented by dentry types that expose parent/name metadata.
type Dentry[D any] interface {
	// VfsDentry is the embedded vfs.Dentry corresponding to this Dentry.
	VfsDentry() *vfs.Dentry

	// parent is the parent of this Dentry in the filesystem's tree. If this
	// Dentry is a filesystem root, parent is nil.
	Parent() *atomic.Pointer[D]

	// name is the name of this Dentry in its parent. If this Dentry is a
	// filesystem root, name is unspecified.
	Name() *string
}

// DentryPtr is a pointer to a dentry type that implements DentryLike.
type DentryPtr[D any] interface {
	Dentry[D]
	~*D
}

// ParentOrSelf returns d.parent. If d.parent is nil, ParentOrSelf returns d.
func ParentOrSelf[D any, T DentryPtr[D]](d T) T {
	if parent := d.Parent().Load(); parent != nil {
		return T(parent)
	}
	return d
}

// SetParentAndName atomically replaces a Dentry's parent and name.
//
// SetParentAndName must be used when changes to a Dentry's parent and name may
// race with observations of the same. If a Dentry is not visible to other
// goroutines (including concurrent calls to PrependPath or IsDescendant) when
// its parent and name are changed, it is safe to either call SetParentAndName
// or mutate d.parent and d.name directly.
func SetParentAndName[D any, T DentryPtr[D]](fs Filesystem, d, newParent T, newName string) {
	mu := fs.AncestryMu()
	mu.Lock()
	defer mu.Unlock()
	d.Parent().Store(newParent)
	*d.Name() = newName
}

// IsAncestorDentry returns true if d is an ancestor of d2; that is, d is
// either d2's parent or an ancestor of d2's parent.
func IsAncestorDentry[D any, T DentryPtr[D]](fs Filesystem, d, d2 T) bool {
	if d == d2 {
		return false
	}
	return IsDescendant(fs, d.VfsDentry(), d2)
}

// IsDescendant returns true if vd is a descendant of vfsroot or if vd and
// vfsroot are the same dentry.
func IsDescendant[D any, T DentryPtr[D]](fs Filesystem, vfsroot *vfs.Dentry, d T) bool {
	mu := fs.AncestryMu()
	mu.RLock()
	defer mu.RUnlock()
	for d != nil && d.VfsDentry() != vfsroot {
		d = T(d.Parent().Load())
	}
	return d != nil
}

// PrependPath is a generic implementation of FilesystemImpl.PrependPath().
func PrependPath[D any, T DentryPtr[D]](fs Filesystem, vfsroot vfs.VirtualDentry, mnt *vfs.Mount, d T, b *fspath.Builder) error {
	mu := fs.AncestryMu()
	mu.RLock()
	defer mu.RUnlock()
	for {
		if mnt == vfsroot.Mount() && d.VfsDentry() == vfsroot.Dentry() {
			return vfs.PrependPathAtVFSRootError{}
		}
		if mnt != nil && d.VfsDentry() == mnt.Root() {
			return nil
		}
		parent := d.Parent().Load()
		if parent == nil {
			return vfs.PrependPathAtNonMountRootError{}
		}
		b.PrependComponent(*d.Name())
		d = T(parent)
	}
}
