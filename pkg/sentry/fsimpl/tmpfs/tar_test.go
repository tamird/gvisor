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

package tmpfs

import (
	"archive/tar"
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"reflect"
	"runtime"
	"strings"
	"testing"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/sentry/contexttest"
	"gvisor.dev/gvisor/pkg/sentry/kernel/auth"
	"gvisor.dev/gvisor/pkg/sentry/vfs"
	"gvisor.dev/gvisor/pkg/usermem"
)

func TestTarXattrRoundTrip(t *testing.T) {
	want := map[string]string{
		"user.empty":  "",
		"user.binary": "\x00\xff\x80\n=",
		"user.\xff":   "non-UTF8 name",
	}
	d := dentry{inode: &inode{impl: &directory{}}}
	d.inode.xattrs.SetRawXattrs(want)
	hdr, err := d.createTarHeader("./", make(map[uint64]string), tarDefaultWriterCallbacks{})
	if err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := tw.WriteHeader(hdr); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	hdr, err = tar.NewReader(&buf).Next()
	if err != nil {
		t.Fatal(err)
	}
	var restored inode
	restored.setXattrsFromPAXRecords(hdr)
	if got := restored.xattrs.RawXattrs(); !reflect.DeepEqual(got, want) {
		t.Errorf("restored xattrs = %q, want %q", got, want)
	}
}

// tarWithEntries returns a tar archive with a root directory and the given
// entries.
func tarWithEntries(t *testing.T, hdrs []*tar.Header, contents [][]byte) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := tw.WriteHeader(&tar.Header{
		Name:     "./",
		Typeflag: tar.TypeDir,
		Mode:     0755,
	}); err != nil {
		t.Fatalf("tar.WriteHeader(dir): %v", err)
	}
	for i, hdr := range hdrs {
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatalf("tar.WriteHeader(%s): %v", hdr.Name, err)
		}
		if _, err := tw.Write(contents[i]); err != nil {
			t.Fatalf("tar.Writer.Write(%s): %v", hdr.Name, err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatalf("tar.Writer.Close: %v", err)
	}
	return &buf
}

// tarWithFile returns a tar archive with a root directory and one regular file.
func tarWithFile(t *testing.T, name string, content []byte) *bytes.Buffer {
	t.Helper()
	return tarWithEntries(t, []*tar.Header{{
		Name:     name,
		Typeflag: tar.TypeReg,
		Mode:     0644,
		Size:     int64(len(content)),
	}}, [][]byte{content})
}

// mountFromTarTestOnly mounts a tmpfs from the tar archive in src. The mount
// is released when the test ends.
func mountFromTarTestOnly(t *testing.T, ctx context.Context, src io.Reader, data string) *filesystem {
	t.Helper()
	creds := auth.CredentialsFromContext(ctx)
	vfsObj := &vfs.VirtualFilesystem{}
	if err := vfsObj.Init(ctx); err != nil {
		t.Fatalf("VFS init: %v", err)
	}
	vfsObj.MustRegisterFilesystemType("tmpfs", FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{
		AllowUserMount: true,
	})
	mntns, err := vfsObj.NewMountNamespace(ctx, creds, "", "tmpfs", &vfs.MountOptions{
		GetFilesystemOptions: vfs.GetFilesystemOptions{
			Data: data,
			InternalData: FilesystemOpts{
				SourceTar: io.NopCloser(src),
			},
		},
	}, nil)
	if err != nil {
		t.Fatalf("NewMountNamespace: %v", err)
	}
	t.Cleanup(func() { mntns.DecRef(ctx) })
	root := mntns.Root(ctx)
	defer root.DecRef(ctx)
	fs, ok := root.Mount().Filesystem().Impl().(*filesystem)
	if !ok {
		t.Fatalf("root filesystem is %T, want *tmpfs.filesystem", root.Mount().Filesystem().Impl())
	}
	return fs
}

// readFileFromTar returns the content of the named regular file in src.
func readFileFromTar(t *testing.T, src io.Reader, name string) []byte {
	t.Helper()
	tr := tar.NewReader(src)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			t.Fatalf("tar archive has no entry for %s", name)
		}
		if err != nil {
			t.Fatalf("tar.Reader.Next: %v", err)
		}
		if hdr.Name != name {
			continue
		}
		got, err := io.ReadAll(tr)
		if err != nil {
			t.Fatalf("reading %s from archive: %v", name, err)
		}
		return got
	}
}

// randomBytes returns size random bytes.
func randomBytes(size int) []byte {
	b := make([]byte, size)
	rand.Read(b)
	return b
}

// TestSourceTarLongSymlinkRelease is a regression test for a pagesUsed
// underflow on teardown. symlinkFromTar did not account the page that decRef
// unaccounts for long symlink targets.
func TestSourceTarLongSymlinkRelease(t *testing.T) {
	ctx := contexttest.Context(t)

	// A target of length shortSymlinkLen takes the long-symlink path.
	src := tarWithEntries(t, []*tar.Header{{
		Name:     "./longlink",
		Typeflag: tar.TypeSymlink,
		Linkname: strings.Repeat("a", shortSymlinkLen),
		Mode:     0777,
	}}, [][]byte{nil})

	// Without the fix, the release on cleanup underflows fs.pagesUsed and panics.
	mountFromTarTestOnly(t, ctx, src, "")
}

// TestTarInvalidACLRelease checks that malformed metadata does not leave
// detached inodes or long-symlink pages charged to the filesystem.
func TestTarInvalidACLRelease(t *testing.T) {
	for _, test := range []struct {
		name   string
		header tar.Header
	}{
		{name: "directory", header: tar.Header{Name: "./entry/", Typeflag: tar.TypeDir}},
		{name: "fifo", header: tar.Header{Name: "./entry", Typeflag: tar.TypeFifo}},
		{name: "block", header: tar.Header{Name: "./entry", Typeflag: tar.TypeBlock, Devmajor: 1, Devminor: 1}},
		{name: "char", header: tar.Header{Name: "./entry", Typeflag: tar.TypeChar, Devmajor: 1, Devminor: 3}},
		{name: "symlink", header: tar.Header{Name: "./entry", Typeflag: tar.TypeSymlink, Linkname: strings.Repeat("a", shortSymlinkLen)}},
		// Regular files already have detached-inode error cleanup.
		{name: "regular", header: tar.Header{Name: "./entry", Typeflag: tar.TypeReg, Size: 1}},
	} {
		for _, acl := range []struct {
			name  string
			xattr string
		}{
			{name: "access", xattr: linux.XATTR_NAME_POSIX_ACL_ACCESS},
			{name: "default", xattr: linux.XATTR_NAME_POSIX_ACL_DEFAULT},
		} {
			t.Run(test.name+"/"+acl.name, func(t *testing.T) {
				ctx := contexttest.Context(t)
				fs := mountFromTarTestOnly(t, ctx, tarWithEntries(t, nil, nil), "")
				inodes, pages := fs.inodesUsed.Load(), fs.pagesUsed.Load()
				hdr := test.header
				hdr.Mode = 0755
				hdr.PAXRecords = map[string]string{paxXattrPrefix + acl.xattr: "!"}
				src := tarWithEntries(t, []*tar.Header{&hdr}, [][]byte{make([]byte, hdr.Size)})
				err := fs.tarRead(ctx, src, tarDefaultReaderCallbacks{})
				var invalid base64.CorruptInputError
				if !errors.As(err, &invalid) {
					t.Fatalf("tarRead: got %v, want invalid ACL encoding", err)
				}
				if got, want := fs.inodesUsed.Load(), inodes; got != want {
					t.Errorf("inodesUsed after invalid ACL = %d, want %d", got, want)
				}
				if got, want := fs.pagesUsed.Load(), pages; got != want {
					t.Errorf("pagesUsed after invalid ACL = %d, want %d", got, want)
				}
			})
		}
	}
}

// TestTarRegularFileRoundTrip checks that file contents survive a restore and
// archive. Sizes straddle the 32 KiB io.Copy buffer.
func TestTarRegularFileRoundTrip(t *testing.T) {
	for _, size := range []int{0, 1, 32<<10 - 1, 32 << 10, 32<<10 + 1, 4<<20 + 1234} {
		t.Run(fmt.Sprint(size), func(t *testing.T) {
			ctx := contexttest.Context(t)
			want := randomBytes(size)
			fs := mountFromTarTestOnly(t, ctx, tarWithFile(t, "./file", want), "")

			var out bytes.Buffer
			if err := fs.tarWrite(ctx, &out, tarDefaultWriterCallbacks{}); err != nil {
				t.Fatalf("tarWrite: %v", err)
			}
			if got := readFileFromTar(t, &out, "./file"); !bytes.Equal(got, want) {
				t.Fatalf("./file content differs after round trip (got %d bytes, want %d)", len(got), len(want))
			}
		})
	}
}

// TestTarRegularFileAllocation checks that restore and archive memory use does
// not scale with file size.
func TestTarRegularFileAllocation(t *testing.T) {
	const (
		fileSize = 4 << 20
		maxAlloc = 1 << 20
	)
	ctx := contexttest.Context(t)
	src := tarWithFile(t, "./file", randomBytes(fileSize))

	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	fs := mountFromTarTestOnly(t, ctx, src, "")
	runtime.ReadMemStats(&after)
	allocated := after.TotalAlloc - before.TotalAlloc
	t.Logf("restoring a %d byte file allocated %d bytes", fileSize, allocated)
	if allocated > maxAlloc {
		t.Errorf("restoring a %d byte file allocated %d bytes, want at most %d", fileSize, allocated, maxAlloc)
	}

	// Discard the archive so that only archiving is measured.
	runtime.ReadMemStats(&before)
	if err := fs.tarWrite(ctx, io.Discard, tarDefaultWriterCallbacks{}); err != nil {
		t.Fatalf("tarWrite: %v", err)
	}
	runtime.ReadMemStats(&after)
	allocated = after.TotalAlloc - before.TotalAlloc
	t.Logf("archiving a %d byte file allocated %d bytes", fileSize, allocated)
	if allocated > maxAlloc {
		t.Errorf("archiving a %d byte file allocated %d bytes, want at most %d", fileSize, allocated, maxAlloc)
	}
}

// TestTarDuplicateRegularFile checks that replacing an entry releases its
// resources before the replacement is allocated, and hard links use the winner.
func TestTarDuplicateRegularFile(t *testing.T) {
	first := randomBytes(5 << 12)
	second := []byte("second")
	for _, test := range []struct {
		name     string
		data     string
		typeflag byte
		mode     uint32
	}{
		// Each version fits, but both versions together exceed the limit.
		{name: "pages", data: fmt.Sprintf("size=%d", len(first)), typeflag: tar.TypeReg, mode: linux.S_IFREG},
		// Two inodes allow the root directory and one file, not two versions.
		{name: "inodes", data: "nr_inodes=2", typeflag: tar.TypeReg, mode: linux.S_IFREG},
		{name: "fifo", data: "nr_inodes=2", typeflag: tar.TypeFifo, mode: linux.S_IFIFO},
		{name: "block", data: "nr_inodes=2", typeflag: tar.TypeBlock, mode: linux.S_IFBLK},
		{name: "char", data: "nr_inodes=2", typeflag: tar.TypeChar, mode: linux.S_IFCHR},
	} {
		t.Run(test.name, func(t *testing.T) {
			ctx := contexttest.Context(t)
			replacement := &tar.Header{Name: "./f", Typeflag: test.typeflag, Mode: 0644}
			var content []byte
			var wantPages uint64
			if test.typeflag == tar.TypeReg {
				content = second
				replacement.Size = int64(len(content))
				wantPages = 1
			}
			src := tarWithEntries(t, []*tar.Header{
				{Name: "./f", Typeflag: tar.TypeReg, Mode: 0644, Size: int64(len(first))},
				replacement,
				{Name: "./alias", Typeflag: tar.TypeLink, Linkname: "./f"},
			}, [][]byte{first, content, nil})
			fs := mountFromTarTestOnly(t, ctx, src, test.data)
			if got := fs.pagesUsed.Load(); got != wantPages {
				t.Errorf("pagesUsed = %d, want %d", got, wantPages)
			}
			if got := fs.inodesUsed.Load(); got != 2 {
				t.Errorf("inodesUsed = %d, want 2", got)
			}
			fs.mu.RLock()
			children := fs.root.inode.impl.(*directory).childMap
			file, alias := children["f"], children["alias"]
			fs.mu.RUnlock()
			if file == nil || alias == nil {
				t.Fatal("restored file or its hard link is missing")
			}
			if got := file.inode.mode.Load() & linux.S_IFMT; got != test.mode {
				t.Errorf("file type = %#o, want %#o", got, test.mode)
			}
			if alias.inode != file.inode {
				t.Error("hard link does not refer to the replacement inode")
			}
			if test.typeflag == tar.TypeReg {
				rf := file.inode.impl.(*regularFile)
				if got := rf.size.Load(); got != uint64(len(content)) {
					t.Errorf("size = %d, want %d", got, len(content))
				}
				got := make([]byte, len(content))
				rw := getRegularFileReadWriter(rf, 0, 0)
				n, err := usermem.BytesIOSequence(got).CopyOutFrom(ctx, rw)
				putRegularFileReadWriter(rw)
				if err != nil || n != int64(len(content)) || !bytes.Equal(got, content) {
					t.Errorf("replacement contents = %q (%d bytes, %v), want %q", got, n, err, content)
				}
			}
		})
	}
}

// TestFSCheckpointRestoreSizeLimit checks that a FS checkpoint restore that
// exceeds the size limit fails without corrupting page accounting.
func TestFSCheckpointRestoreSizeLimit(t *testing.T) {
	ctx := contexttest.Context(t)
	src := mountFromTarTestOnly(t, ctx, tarWithFile(t, "./f", randomBytes(16<<12)), "")
	var ckpt bytes.Buffer
	if err := FSCheckpointWrite(ctx, &src.vfsfs, &ckpt); err != nil {
		t.Fatalf("FSCheckpointWrite: %v", err)
	}
	vfsObj := &vfs.VirtualFilesystem{}
	if err := vfsObj.Init(ctx); err != nil {
		t.Fatalf("VFS init: %v", err)
	}
	vfsObj.MustRegisterFilesystemType("tmpfs", FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{
		AllowUserMount: true,
	})
	if _, err := vfsObj.NewMountNamespace(ctx, auth.CredentialsFromContext(ctx), "", "tmpfs", &vfs.MountOptions{
		GetFilesystemOptions: vfs.GetFilesystemOptions{
			Data: "size=16k",
			InternalData: FilesystemOpts{
				SourceTar:             io.NopCloser(&ckpt),
				SourceTarFSCheckpoint: true,
			},
		},
	}, nil); err == nil {
		t.Fatalf("NewMountNamespace succeeded, want error")
	}
}
