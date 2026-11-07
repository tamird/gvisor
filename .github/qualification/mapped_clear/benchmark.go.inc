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

package pgalloc

import (
	"bytes"
	"os"
	"strconv"
	"testing"

	"golang.org/x/sys/cpu"
	"golang.org/x/sys/unix"
)

func BenchmarkZeroDiskBacked64MiB(b *testing.B) {
	const size = 64 << 20
	b.StopTimer()
	core, err := strconv.Atoi(os.Getenv("CLEAR_CPU"))
	if err != nil {
		b.Fatal(err)
	}
	var affinity unix.CPUSet
	if err := unix.SchedGetaffinity(0, &affinity); err != nil || affinity.Count() != 1 || !affinity.IsSet(core) || !cpu.X86.HasERMS {
		b.Fatalf("cpu %d: affinity=%v ERMS=%t: %v", core, affinity, cpu.X86.HasERMS, err)
	}
	f, err := os.OpenFile(os.Getenv("CLEAR_FILE"), os.O_RDWR, 0)
	if err != nil {
		b.Fatal(err)
	}
	defer f.Close()
	var st unix.Stat_t
	if err := unix.Fstat(int(f.Fd()), &st); err != nil || st.Mode&unix.S_IFMT != unix.S_IFREG || st.Size != size || st.Blocks*512 < size {
		b.Fatalf("expected an allocated regular 64 MiB file: %v", err)
	}
	var fs unix.Statfs_t
	if err := unix.Fstatfs(int(f.Fd()), &fs); err != nil || fs.Type == unix.TMPFS_MAGIC || fs.Type == unix.RAMFS_MAGIC {
		b.Fatalf("expected a disk-backed file, type %#x: %v", fs.Type, err)
	}
	m, err := unix.Mmap(int(f.Fd()), 0, size, unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
	if err != nil {
		b.Fatal(err)
	}
	defer unix.Munmap(m)
	zeros := make([]byte, size)
	b.SetBytes(size)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		for j := range m {
			m[j] = 0xa5
		}
		b.StartTimer()
		ok := zeroDiskBacked(int(f.Fd()), 0, m)
		b.StopTimer()
		if !ok || !bytes.Equal(m, zeros) {
			b.Fatal("protected clear left a nonzero or unwritable page")
		}
	}
	b.Logf("MAPPED_CLEAR cpu=%d erms=%t dev=%d ino=%d fstype=%x verified=%d size=%d", core, cpu.X86.HasERMS, st.Dev, st.Ino, fs.Type, b.N, size)
}
