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

package seqatomic

import (
	"sync/atomic"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/sync"
)

func TestLoadUncontended(t *testing.T) {
	var seq sync.SeqCount
	const want = 1
	data := want
	if got := Load(&seq, &data); got != want {
		t.Errorf("Load: got %v, wanted %v", got, want)
	}
}

func TestLoadAfterWrite(t *testing.T) {
	var seq sync.SeqCount
	var data int
	const want = 1
	seq.BeginWrite()
	data = want
	seq.EndWrite()
	if got := Load(&seq, &data); got != want {
		t.Errorf("Load: got %v, wanted %v", got, want)
	}
}

func TestLoadDuringWrite(t *testing.T) {
	var seq sync.SeqCount
	var data int
	const want = 1
	seq.BeginWrite()
	go func() {
		time.Sleep(time.Second)
		data = want
		seq.EndWrite()
	}()
	if got := Load(&seq, &data); got != want {
		t.Errorf("Load: got %v, wanted %v", got, want)
	}
}

func TestTryLoadUncontended(t *testing.T) {
	var seq sync.SeqCount
	const want = 1
	data := want
	epoch := seq.BeginRead()
	if got, ok := TryLoad(&seq, epoch, &data); !ok || got != want {
		t.Errorf("TryLoad: got (%v, %v), wanted (%v, true)", got, ok, want)
	}
}

func TestTryLoadDuringWrite(t *testing.T) {
	var seq sync.SeqCount
	var data int
	epoch := seq.BeginRead()
	seq.BeginWrite()
	if got, ok := TryLoad(&seq, epoch, &data); ok {
		t.Errorf("TryLoad: got (%v, true), wanted (_, false)", got)
	}
	seq.EndWrite()
}

func TestTryLoadAfterWrite(t *testing.T) {
	var seq sync.SeqCount
	var data int
	epoch := seq.BeginRead()
	seq.BeginWrite()
	seq.EndWrite()
	if got, ok := TryLoad(&seq, epoch, &data); ok {
		t.Errorf("TryLoad: got (%v, true), wanted (_, false)", got)
	}
}

func BenchmarkLoadIntUncontended(b *testing.B) {
	var seq sync.SeqCount
	const want = 42
	data := want
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			if got := Load(&seq, &data); got != want {
				b.Fatalf("Load: got %v, wanted %v", got, want)
			}
		}
	})
}

func BenchmarkTryLoadIntUncontended(b *testing.B) {
	var seq sync.SeqCount
	const want = 42
	data := want
	b.RunParallel(func(pb *testing.PB) {
		epoch := seq.BeginRead()
		for pb.Next() {
			if got, ok := TryLoad(&seq, epoch, &data); !ok || got != want {
				b.Fatalf("TryLoad: got (%v, %v), wanted (%v, true)", got, ok, want)
			}
		}
	})
}

// For comparison:
func BenchmarkAtomicPointerLoadIntUncontended(b *testing.B) {
	var a atomic.Pointer[int]
	const want = 42
	value := int(want)
	a.Store(&value)
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			if got := a.Load(); *got != want {
				b.Fatalf("atomic.Pointer[int].Load: got %v, wanted %v", got, want)
			}
		}
	})
}
