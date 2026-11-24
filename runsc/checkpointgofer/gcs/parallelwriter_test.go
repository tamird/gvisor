// Copyright 2025 The gVisor Authors.
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

package gcs

import (
	"context"
	"fmt"
	"os"
	"slices"
	"strings"
	"testing"

	"cloud.google.com/go/storage"
	"google.golang.org/api/option"
	"gvisor.dev/gvisor/pkg/sentry/state/stateio"
)

func (w *ParallelWriter) indicesFromPartName(name string) (levelIndex int, partIndex uint64, ok bool) {
	if len(name) < w.tmpFmtPrefixLen {
		return
	}
	n, _ := fmt.Sscanf(name[w.tmpFmtPrefixLen:], "%d_%x", &levelIndex, &partIndex)
	ok = n == 2
	return
}

// TestParallelWriterWriteCompositionGraph tests that the composition graph
// built during writing is correct.
func TestParallelWriterWriteCompositionGraph(t *testing.T) {
	enableGCS := disableGCS(t)
	defer enableGCS()

	ctx := context.Background()
	client, err := storage.NewClient(ctx, option.WithoutAuthentication())
	if err != nil {
		t.Fatalf("storage.NewClient failed: %v", err)
	}
	bkt := client.Bucket("test-bucket")

	w := &ParallelWriter{
		bucket: bkt,
		obj:    bkt.Object("test-object"),
	}
	w.initTmpFmt()

	// - Check that each leaf has level index 0 and a unique part index that is
	// within the range [0, maxLeafIndex]. Additionally check that the total
	// number of leaves is exactly maxLeafIndex+1. This verifies that we have
	// exactly the set of expected leaves.
	//
	// - Check that each branch has a non-zero level index, and that its parts
	// have the level and part indices expected given the branch's level and
	// part indices. This, in conjunction with verifying leaves, verifies that
	// we have exactly the set of expected branches with the expected
	// connectivity.

	seenLeaves := make(map[uint64]*composeLeaf)
	highestPartIndex := make(map[int]uint64)
	var visit func(*composeNode, int, uint64, uint64)
	visit = func(node *composeNode, wantLevelIndex int, wantPartIndex, maxLeafIndex uint64) {
		name := node.obj.ObjectName()
		levelIndex, partIndex, ok := w.indicesFromPartName(name)
		if !ok {
			t.Errorf("failed to parse %q as a part name", name)
			return
		}
		if levelIndex != wantLevelIndex || partIndex != wantPartIndex {
			t.Errorf("node %q has indices (%d, %#x=%d), want (%d, %#x=%d)", name, levelIndex, partIndex, partIndex, wantLevelIndex, wantPartIndex, wantPartIndex)
		}
		highestPartIndex[levelIndex] = max(highestPartIndex[levelIndex], partIndex)
		switch node := node.impl.(type) {
		case *composeLeaf:
			if levelIndex != 0 {
				t.Errorf("leaf %q found at level %d, want 0", name, levelIndex)
			} else {
				if partIndex > maxLeafIndex {
					t.Errorf("leaf %q has part index %#x=%d, want <= %d", name, partIndex, partIndex, maxLeafIndex)
				}
				if old := seenLeaves[partIndex]; old != nil {
					t.Errorf("duplicate leaves %q (%p) and %q (%p) at part index %d", old.obj.ObjectName(), old, name, node, partIndex)
				}
				seenLeaves[partIndex] = node
			}
		case *composeBranch:
			if levelIndex == 0 {
				t.Errorf("branch %q found at level 0", name)
			}
			if node.partsLen < 2 || node.partsLen > composeMax {
				t.Errorf("branch %q has %d parts, want 2-%d", name, node.partsLen, composeMax)
			}
			wantFirstPartIndex := partIndex * composeMax
			for i, part := range node.parts[:node.partsLen] {
				part.readyMu.Lock()
				parent := part.parent
				part.readyMu.Unlock()
				if parent == nil {
					t.Errorf("branch %q has part %q with nil parent", name, part.obj.ObjectName())
				} else if parent != node {
					t.Errorf("branch %q (%p) has part %q with different parent %q (%p)", name, node, part.obj.ObjectName(), parent.obj.ObjectName(), parent)
				}
				visit(part, wantLevelIndex-1, wantFirstPartIndex+uint64(i), maxLeafIndex)
			}
		default:
			panic(fmt.Sprintf("unknown composeNode.impl %T", node))
		}
	}

	for numWritesMinusOne := range composeMax*composeMax + composeMax + 2 {
		leaf := w.makeLeaf(0, stateio.LocalClientRanges{})
		w.insertIntoWriteCompositionGraph(leaf)

		clear(seenLeaves)
		clear(highestPartIndex)
		for levelIndex, v := range slices.Backward(w.levels) {
			if node := v.firstPart; node != nil {
				highestPartIndexForLevel, ok := highestPartIndex[levelIndex]
				nextPartIndex := uint64(0)
				if ok {
					nextPartIndex = highestPartIndexForLevel + 1
				}
				visit(node, levelIndex, nextPartIndex, uint64(numWritesMinusOne))
			}
		}
		if got, want := len(seenLeaves), numWritesMinusOne+1; got != want {
			t.Errorf("got %d leaves, want %d", got, want)
		}
	}
}

type testComposeNode struct {
	parts      []*testComposeNode
	levelIndex int // -1 for root
	partIndex  uint64
}

func (n *testComposeNode) name() string {
	return fmt.Sprintf("%d_%08x", n.levelIndex, n.partIndex)
}

func (n *testComposeNode) partNames() string {
	var b strings.Builder
	b.WriteByte('[')
	var sep string
	for _, part := range n.parts {
		b.WriteString(sep)
		b.WriteString(part.name())
		sep = " "
	}
	b.WriteByte(']')
	return b.String()
}

func testComposeRange(levelIndex int, firstPartIndex, lastPartIndex uint64) []*testComposeNode {
	n := lastPartIndex - firstPartIndex + 1
	nodes := make([]*testComposeNode, n)
	for i := range nodes {
		nodes[i] = &testComposeNode{levelIndex: levelIndex, partIndex: firstPartIndex + uint64(i), parts: nil}
	}
	return nodes
}

func checkComposeTree(t *testing.T, w *ParallelWriter, gotNode *composeNode, wantNode *testComposeNode) {
	name := gotNode.obj.ObjectName()
	levelIndex, partIndex, ok := w.indicesFromPartName(name)
	if !ok {
		if wantNode.levelIndex >= 0 {
			t.Errorf("got node %q, want non-root with indices (%d, %#x=%d)", name, wantNode.levelIndex, wantNode.partIndex, wantNode.partIndex)
		}
	} else {
		if wantNode.levelIndex < 0 {
			t.Errorf("got node %q with indices (%d, %#x=%d), want root", name, levelIndex, partIndex, partIndex)
		} else if levelIndex != wantNode.levelIndex || partIndex != wantNode.partIndex {
			t.Errorf("got node %q with indices (%d, %#x=%d), want (%d, %#x=%d)", name, levelIndex, partIndex, partIndex, wantNode.levelIndex, wantNode.partIndex, wantNode.partIndex)
		}
	}
	switch gotNode := gotNode.impl.(type) {
	case *composeLeaf:
		if len(wantNode.parts) != 0 {
			t.Errorf("got leaf %q, want branch with %d parts %s", name, len(wantNode.parts), wantNode.partNames())
		}
	case *composeBranch:
		if int(gotNode.partsLen) != len(wantNode.parts) {
			t.Errorf("got branch %q with %d parts %s, want branch with %d parts %s", name, gotNode.partsLen, w.nameTempObjsForLogging(appendNodeObjs(gotNode.parts[:gotNode.partsLen], nil), gotNode.mixed), len(wantNode.parts), wantNode.partNames())
		} else {
			for i := range gotNode.partsLen {
				checkComposeTree(t, w, gotNode.parts[i], wantNode.parts[i])
			}
		}
	default:
		panic(fmt.Sprintf("unknown composeNode.impl %T", gotNode))
	}
}

// TestParallelWriterFinalCompositionTree tests that composition trees built
// during finalization are correct.
func TestParallelWriterFinalCompositionTree(t *testing.T) {
	enableGCS := disableGCS(t)
	defer enableGCS()

	ctx := context.Background()
	client, err := storage.NewClient(ctx, option.WithoutAuthentication())
	if err != nil {
		t.Fatalf("storage.NewClient failed: %v", err)
	}
	bkt := client.Bucket("test-bucket")

	type testCase struct {
		wantRoot  *testComposeNode
		numWrites int
	}
	for _, tc := range []testCase{
		{numWrites: 2, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: []*testComposeNode{
			&testComposeNode{levelIndex: 0, partIndex: 0, parts: nil},
			&testComposeNode{levelIndex: 0, partIndex: 1, parts: nil}}}},
		{numWrites: 31, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0,
			parts: testComposeRange(0, 0, 30)}},
		// At 32 writes (and any number of writes that is a non-negative
		// integer power of 32), there is only one temporary object, which is
		// moved to the final destination rather than being composed.
		{numWrites: 33, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: []*testComposeNode{
			&testComposeNode{levelIndex: 1, partIndex: 0, parts: nil},
			&testComposeNode{levelIndex: 0, partIndex: 32, parts: nil}}}},
		{numWrites: 63, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: append([]*testComposeNode{
			&testComposeNode{levelIndex: 1, partIndex: 0, parts: nil}},
			testComposeRange(0, 32, 62)...)}},
		{numWrites: 64, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: []*testComposeNode{
			&testComposeNode{levelIndex: 1, partIndex: 0, parts: nil},
			&testComposeNode{levelIndex: 1, partIndex: 1, parts: nil}}}},
		{numWrites: 94, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: append([]*testComposeNode{
			&testComposeNode{levelIndex: 1, partIndex: 0, parts: nil},
			&testComposeNode{levelIndex: 1, partIndex: 1, parts: nil}},
			testComposeRange(0, 64, 93)...)}},
		// Minimum number of writes needed for one sub-maximal non-root
		// composition: 31+2=33 source objects, grouped as 2+31 in the write
		// composition graph.
		{numWrites: 95, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: append(append([]*testComposeNode{
			&testComposeNode{levelIndex: 1, partIndex: 0, parts: nil},
			&testComposeNode{levelIndex: 1, partIndex: 1, parts: nil}},
			testComposeRange(0, 64, 92)...),
			&testComposeNode{levelIndex: 2, partIndex: 0, parts: []*testComposeNode{
				&testComposeNode{levelIndex: 0, partIndex: 93, parts: nil},
				&testComposeNode{levelIndex: 0, partIndex: 94, parts: nil}}})}},
		{numWrites: 1023, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: append(
			testComposeRange(1, 0, 30),
			&testComposeNode{levelIndex: 2, partIndex: 0,
				parts: testComposeRange(0, 992, 1022)})}},
		// 1024 == 32**2
		{numWrites: 1025, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: []*testComposeNode{
			&testComposeNode{levelIndex: 2, partIndex: 0, parts: nil},
			&testComposeNode{levelIndex: 0, partIndex: 1024, parts: nil}}}},
		{numWrites: 1056, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: []*testComposeNode{
			&testComposeNode{levelIndex: 2, partIndex: 0, parts: nil},
			&testComposeNode{levelIndex: 1, partIndex: 32, parts: nil}}}},
		{numWrites: 1086, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: append([]*testComposeNode{
			&testComposeNode{levelIndex: 2, partIndex: 0, parts: nil},
			&testComposeNode{levelIndex: 1, partIndex: 32, parts: nil}},
			testComposeRange(0, 1056, 1085)...)}},
		{numWrites: 1087, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: append(append([]*testComposeNode{
			&testComposeNode{levelIndex: 2, partIndex: 0, parts: nil},
			&testComposeNode{levelIndex: 1, partIndex: 32, parts: nil}},
			testComposeRange(0, 1056, 1084)...),
			&testComposeNode{levelIndex: 3, partIndex: 0, parts: []*testComposeNode{
				&testComposeNode{levelIndex: 0, partIndex: 1085, parts: nil},
				&testComposeNode{levelIndex: 0, partIndex: 1086, parts: nil}}})}},
		// Minimum number of writes needed for one maximal non-root
		// composition: 31+32=63 source objects, grouped as 1+31+31 in the
		// write composition graph.
		{numWrites: 2047, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: append(append([]*testComposeNode{
			&testComposeNode{levelIndex: 2, partIndex: 0, parts: nil}},
			testComposeRange(1, 32, 61)...),
			&testComposeNode{levelIndex: 3, partIndex: 0, parts: append([]*testComposeNode{
				&testComposeNode{levelIndex: 1, partIndex: 62, parts: nil}},
				testComposeRange(0, 2016, 2046)...)})}},
		{numWrites: 3040, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: append(append([]*testComposeNode{
			&testComposeNode{levelIndex: 2, partIndex: 0, parts: nil},
			&testComposeNode{levelIndex: 2, partIndex: 1, parts: nil}},
			testComposeRange(1, 64, 92)...),
			&testComposeNode{levelIndex: 3, partIndex: 0, parts: []*testComposeNode{
				&testComposeNode{levelIndex: 1, partIndex: 93, parts: nil},
				&testComposeNode{levelIndex: 1, partIndex: 94, parts: nil}}})}},
		{numWrites: 3070, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: append(append([]*testComposeNode{
			&testComposeNode{levelIndex: 2, partIndex: 0, parts: nil},
			&testComposeNode{levelIndex: 2, partIndex: 1, parts: nil}},
			testComposeRange(1, 64, 92)...),
			&testComposeNode{levelIndex: 3, partIndex: 0, parts: append([]*testComposeNode{
				&testComposeNode{levelIndex: 1, partIndex: 93, parts: nil},
				&testComposeNode{levelIndex: 1, partIndex: 94, parts: nil}},
				testComposeRange(0, 3040, 3069)...)})}},
		// Minimum number of writes needed for one maximal + one sub-maximal
		// non-root composition: 30+2+32=64 source objects, grouped as 2+31+31
		// in the write composition graph.
		{numWrites: 3071, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: append(append([]*testComposeNode{
			&testComposeNode{levelIndex: 2, partIndex: 0, parts: nil},
			&testComposeNode{levelIndex: 2, partIndex: 1, parts: nil}},
			testComposeRange(1, 64, 91)...),
			&testComposeNode{levelIndex: 3, partIndex: 0, parts: []*testComposeNode{
				&testComposeNode{levelIndex: 1, partIndex: 92, parts: nil},
				&testComposeNode{levelIndex: 1, partIndex: 93, parts: nil}}},
			&testComposeNode{levelIndex: 3, partIndex: 1, parts: append([]*testComposeNode{
				&testComposeNode{levelIndex: 1, partIndex: 94, parts: nil}},
				testComposeRange(0, 3040, 3070)...)})}},
		// This is the example given in buildFinalCompositionTree().
		{numWrites: 5119, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: append(append(
			testComposeRange(2, 0, 3),
			testComposeRange(1, 128, 153)...),
			&testComposeNode{levelIndex: 3, partIndex: 0,
				parts: testComposeRange(1, 154, 157)},
			&testComposeNode{levelIndex: 3, partIndex: 1, parts: append([]*testComposeNode{
				&testComposeNode{levelIndex: 1, partIndex: 158, parts: nil}},
				testComposeRange(0, 5088, 5118)...)})}},
		// Minimum number of writes needed for two maximal non-root
		// compositions: 30+32+32=94 source objects, grouped as 1+31+31+31 in
		// the write composition graph.
		{numWrites: 65535, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: append(append([]*testComposeNode{
			&testComposeNode{levelIndex: 3, partIndex: 0, parts: nil}},
			testComposeRange(2, 32, 60)...),
			&testComposeNode{levelIndex: 4, partIndex: 0, parts: append([]*testComposeNode{
				&testComposeNode{levelIndex: 2, partIndex: 61, parts: nil},
				&testComposeNode{levelIndex: 2, partIndex: 62, parts: nil}},
				testComposeRange(1, 2016, 2045)...)},
			&testComposeNode{levelIndex: 4, partIndex: 1, parts: append([]*testComposeNode{
				&testComposeNode{levelIndex: 1, partIndex: 2046, parts: nil}},
				testComposeRange(0, 65504, 65534)...)})}},
		// Minimum number of writes needed for two maximal + one sub-maximal
		// non-root compositions: 29+2+32+32=95 source objects, grouped as
		// 2+31+31+31 in the write composition graph.
		{numWrites: 98303, wantRoot: &testComposeNode{levelIndex: -1, partIndex: 0, parts: append(append([]*testComposeNode{
			&testComposeNode{levelIndex: 3, partIndex: 0, parts: nil},
			&testComposeNode{levelIndex: 3, partIndex: 1, parts: nil}},
			testComposeRange(2, 64, 90)...),
			&testComposeNode{levelIndex: 4, partIndex: 0, parts: []*testComposeNode{
				&testComposeNode{levelIndex: 2, partIndex: 91, parts: nil},
				&testComposeNode{levelIndex: 2, partIndex: 92, parts: nil}}},
			&testComposeNode{levelIndex: 4, partIndex: 1, parts: append([]*testComposeNode{
				&testComposeNode{levelIndex: 2, partIndex: 93, parts: nil},
				&testComposeNode{levelIndex: 2, partIndex: 94, parts: nil}},
				testComposeRange(1, 3040, 3069)...)},
			&testComposeNode{levelIndex: 4, partIndex: 2, parts: append([]*testComposeNode{
				&testComposeNode{levelIndex: 1, partIndex: 3070, parts: nil}},
				testComposeRange(0, 98272, 98302)...)})}},
	} {
		t.Run(fmt.Sprintf("%d", tc.numWrites), func(t *testing.T) {
			w := &ParallelWriter{
				bucket: bkt,
				obj:    bkt.Object("test-object"),
			}
			w.initTmpFmt()
			for range tc.numWrites {
				leaf := w.makeLeaf(0, stateio.LocalClientRanges{})
				w.insertIntoWriteCompositionGraph(leaf)
				leaf.readyMu.Lock()
				leaf.ready = true // required by w.getEventuallyReadyNodes()
				leaf.readyMu.Unlock()
			}
			nodes := w.getEventuallyReadyNodes()
			if len(nodes) <= 1 {
				t.Fatalf("getEventuallyReadyNodes returned %d nodes %s, want 2+", len(nodes), w.nameTempObjsForLogging(appendNodeObjs(nodes, nil), true /* mixed */))
			}
			// This test is only concerned with compositions formed during
			// finalization, which is reflected in compositions formed during
			// write being omitted from tc.wantRoot for brevity. Forget write
			// compositions here so that comparisons of the two succeed.
			for _, node := range nodes {
				if branch, ok := node.impl.(*composeBranch); ok {
					branch.partsLen = 0
				}
			}
			levelBase := len(w.levels) - 1
			root, _ := w.buildFinalCompositionTree(nodes, levelBase)
			checkComposeTree(t, w, root.node(), tc.wantRoot)
		})
	}
}

func disableGCS(t *testing.T) func() {
	// This is called by tests that shouldn't send any actual requests to GCS
	// (which as of this writing is all of them...) Set STORAGE_EMULATOR_HOST
	// (which overrides the server that storage.Client connects to) to ensure
	// that this is the case.
	const sehKey = "STORAGE_EMULATOR_HOST"
	oldSEH, haveOldSEH := os.LookupEnv(sehKey)
	if err := os.Setenv(sehKey, "localhost:65535"); err != nil {
		t.Fatalf("Failed to override %v: %v", sehKey, err)
	}
	return func() {
		if haveOldSEH {
			if err := os.Setenv(sehKey, oldSEH); err != nil {
				t.Errorf("Failed to reset %v to %q: %v", sehKey, oldSEH, err)
			}
		} else {
			if err := os.Unsetenv(sehKey); err != nil {
				t.Errorf("Failed to unset %v: %v", sehKey, err)
			}
		}
	}
}
