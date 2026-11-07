// Fork-only measurement of the descriptor allocated by inode.Open.
package fuse

import (
	"bytes"
	"context"
	"testing"
	"unsafe"

	"gvisor.dev/gvisor/pkg/state"
)

var measuredRegularFile *regularFileFD

func BenchmarkFUSERegularFile(b *testing.B) {
	b.ReportAllocs()
	for b.Loop() {
		measuredRegularFile = new(regularFileFD)
	}
}

func TestFUSEDescriptorCheckpoint(t *testing.T) {
	original := state.GraphEngineEnabled()
	t.Cleanup(func() { state.EnableGraphEngine(original) })
	for _, tc := range []struct {
		name  string
		graph bool
	}{{"wire", false}, {"graph", true}} {
		t.Run(tc.name, func(t *testing.T) {
			state.EnableGraphEngine(tc.graph)
			// Isolate descriptor-local state. TestFUSEReadError covers opening
			// the file and reading data through its FUSE server.
			src := regularFileFD{off: 12345}
			src.Fh = 91
			src.DirectIO = true
			src.OpenFlag = 17
			var saved bytes.Buffer
			if _, err := state.Save(context.Background(), &saved, &src); err != nil {
				t.Fatal(err)
			}
			size := saved.Len()
			var dst regularFileFD
			if _, err := state.Load(context.Background(), &saved, &dst); err != nil {
				t.Fatal(err)
			}
			dst.offMu.Lock()
			off := dst.off
			dst.offMu.Unlock()
			if off != 12345 || dst.Fh != 91 || !dst.DirectIO || dst.OpenFlag != 17 {
				t.Fatalf("restored offset %d handle %d direct %v flags %d", off, dst.Fh, dst.DirectIO, dst.OpenFlag)
			}
			t.Logf("FUSE-STORAGE codec=%s struct=%d checkpoint=%d offset=%d handle=%d", tc.name, unsafe.Sizeof(src), size, off, dst.Fh)
		})
	}
}
