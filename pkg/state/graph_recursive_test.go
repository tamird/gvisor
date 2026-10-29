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

package state

import (
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"reflect"
	"runtime/debug"
	"strings"
	"testing"
)

type recursiveShape []recursiveShape

type recursiveNamed []recursiveNamed

func (*recursiveNamed) StateTypeName() string { return "state.testRecursiveNamed" }
func (*recursiveNamed) StateFields() []string { return nil }

func init() {
	Register((*recursiveNamed)(nil))
}

func TestGraphRecursiveDescriptor(t *testing.T) {
	const child = "GVISOR_RECURSIVE_DESCRIPTOR_CHILD"
	if os.Getenv(child) == "1" {
		// A fatal runtime stack overflow cannot be caught by Save's error
		// boundary. Bound the child stack; never crash the parent test.
		debug.SetMaxStack(256 << 10)
		EnableGraphEngine(true)
		var v recursiveShape
		var out bytes.Buffer
		_, err := Save(context.Background(), &out, &v)
		var stateErr *ErrState
		if !errors.As(err, &stateErr) || !strings.Contains(stateErr.Error(), "recursive type") || out.Len() != 0 {
			t.Fatalf("graph Save: bytes=%d, err=%v; want a recursive type error before writing", out.Len(), err)
		}
		t.Log("recursive descriptor returned an error without emitting a partial frame")
		return
	}

	old := GraphEngineEnabled()
	defer EnableGraphEngine(old)
	t.Run("type_info", func(t *testing.T) {
		ti := typeInfoOf(reflect.TypeFor[recursiveShape]())
		if ti.elem != ti || ti.registered {
			t.Fatal("unregistered slice's typeInfo does not link to itself")
		}
	})
	t.Run("legacy_nil_root", func(t *testing.T) {
		EnableGraphEngine(false)
		var src, dst recursiveShape
		var frame bytes.Buffer
		if _, err := Save(context.Background(), &frame, &src); err != nil {
			t.Fatalf("legacy Save: %v", err)
		}
		if _, err := Load(context.Background(), &frame, &dst); err != nil || dst != nil || frame.Len() != 0 {
			t.Fatalf("legacy Load: nil=%t, remaining=%d, err=%v", dst == nil, frame.Len(), err)
		}
	})
	EnableGraphEngine(true)
	t.Run("registered_cycle", func(t *testing.T) {
		src := make(recursiveNamed, 2)
		src[0], src[1] = src, src
		var out recursiveNamed
		var frame bytes.Buffer
		if _, err := Save(context.Background(), &frame, &src); err != nil {
			t.Fatalf("Save: %v", err)
		}
		if _, err := Load(context.Background(), &frame, &out); err != nil {
			t.Fatalf("Load: %v", err)
		}
		if frame.Len() != 0 || len(out) != 2 || len(out[0]) != 2 || len(out[1]) != 2 || &out[0][0] != &out[0] || &out[1][0] != &out[0] {
			t.Fatal("restored references do not refer to their root backing array")
		}
		out[1][0] = nil
		if out[0] != nil {
			t.Fatal("mutation did not traverse restored alias")
		}
	})
	t.Run("unregistered_acyclic", func(t *testing.T) {
		type numbers []int
		src := numbers{7, 11}
		var dst numbers
		var frame bytes.Buffer
		if _, err := Save(context.Background(), &frame, &src); err != nil {
			t.Fatal(err)
		}
		if _, err := Load(context.Background(), &frame, &dst); err != nil || !reflect.DeepEqual(src, dst) || frame.Len() != 0 {
			t.Fatalf("Load: dst=%v remaining=%d err=%v", dst, frame.Len(), err)
		}
	})
	t.Run("unregistered_cycle", func(t *testing.T) {
		self, err := os.Executable()
		if err != nil {
			t.Fatal(err)
		}
		cmd := exec.Command(self, "-test.v", "-test.run=^TestGraphRecursiveDescriptor$")
		for _, e := range os.Environ() {
			if !strings.HasPrefix(e, "XML_OUTPUT_FILE=") && !strings.HasPrefix(e, "TEST_PREMATURE_EXIT_FILE=") {
				cmd.Env = append(cmd.Env, e)
			}
		}
		cmd.Env = append(cmd.Env, child+"=1")
		b, err := cmd.CombinedOutput()
		if err != nil {
			if bytes.Contains(b, []byte("fatal error: stack overflow")) && bytes.Contains(b, []byte("(*typeTable).add(")) {
				t.Fatal("bounded child hit a runtime stack overflow in typeTable.add while saving a nil unregistered recursive slice")
			}
			t.Fatalf("child failed unexpectedly: %v; output prefix %s", err, b[:min(len(b), 400)])
		}
		if !bytes.Contains(b, []byte("recursive descriptor returned an error without emitting a partial frame")) {
			t.Fatal("missing child result")
		}
	})
}
