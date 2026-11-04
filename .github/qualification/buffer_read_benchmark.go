// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package buffer

import (
	"bytes"
	"testing"
)

func readAtMultiViewFixture(t testing.TB) (*Buffer, []byte, []byte) {
	t.Helper()
	buf := new(Buffer)
	t.Cleanup(buf.Release)
	want := make([]byte, 512)
	for i := range want {
		want[i] = byte(i)
	}
	for offset := 0; offset < len(want); offset += 64 {
		buf.appendOwned(NewViewWithData(want[offset : offset+64]))
	}
	views := 0
	for v := buf.data.Front(); v != nil; v = v.Next() {
		views++
	}
	if views != 8 {
		t.Fatalf("views = %d, want 8", views)
	}
	p := make([]byte, 400)
	if n, err := buf.ReadAt(p, 10); n != len(p) || err != nil || !bytes.Equal(p, want[10:410]) {
		t.Fatalf("ReadAt = (%d, %v), want (400, nil) and exact payload", n, err)
	}
	t.Log("BUFFER_READ_CONTROL views=8 bytes=512 read=400 offset=10")
	return buf, p, want
}

func TestReadAtMultiViewQualification(t *testing.T) {
	readAtMultiViewFixture(t)
}

// BenchmarkReadAtMultiView reads across independently allocated fragments.
// Setup and correctness checks are not timed.
func BenchmarkReadAtMultiView(b *testing.B) {
	buf, p, want := readAtMultiViewFixture(b)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if n, err := buf.ReadAt(p, 10); n != len(p) || err != nil {
			b.Fatalf("ReadAt = (%d, %v), want (400, nil)", n, err)
		}
	}
	b.StopTimer()
	if !bytes.Equal(p, want[10:410]) {
		b.Fatal("ReadAt changed the expected payload")
	}
}

var qualificationBytes int

func BenchmarkViewListAsSlice(b *testing.B) {
	buf, _, _ := readAtMultiViewFixture(b)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		n := 0
		for v := buf.data.Front(); v != nil; v = v.Next() {
			n += len(v.AsSlice())
		}
		qualificationBytes = n
	}
	b.StopTimer()
	if qualificationBytes != 512 {
		b.Fatalf("view bytes = %d, want 512", qualificationBytes)
	}
}
