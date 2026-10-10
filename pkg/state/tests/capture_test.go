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

package tests

import (
	"bytes"
	"math"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/state"
)

func TestCapturedLegacyWire(t *testing.T) {
	for _, scalar := range []struct {
		name  string
		value float32
	}{
		{"ordinary", -1.5},
		{"positive_signaling_nan", math.Float32frombits(0x7f800001)},
		{"negative_signaling_nan", math.Float32frombits(0xff800001)},
		{"negative_zero", math.Float32frombits(0x80000000)},
	} {
		t.Run(scalar.name, func(t *testing.T) {
			original := captureRecord{enabled: true, signed: -32768, unsigned: ^uint64(0), text: "owned bytes", child: inner{v: 987}, float32: scalar.value, complex64: complex(scalar.value, scalar.value), elapsed: time.Second}
			original.link = &original.child
			original.again = original.link
			var legacy, captured bytes.Buffer
			if _, err := state.Save(t.Context(), &legacy, &original); err != nil {
				t.Fatal(err)
			}
			if _, err := state.SaveCaptured(t.Context(), &captured, &original); err != nil {
				t.Fatal(err)
			}
			if got, want := captured.Bytes(), legacy.Bytes(); !bytes.Equal(got, want) {
				t.Fatalf("captured bytes = %x, want legacy %x", got, want)
			}
			var loaded captureRecord
			if _, err := state.Load(t.Context(), &captured, &loaded); err != nil {
				t.Fatal(err)
			}
			if got, want := loaded.link, &loaded.child; got != want {
				t.Errorf("interior pointer = %p, want %p", got, want)
			}
			if got, want := loaded.again, loaded.link; got != want {
				t.Errorf("repeated pointer = %p, want %p", got, want)
			}
			if got, want := loaded.child.v, original.child.v; got != want {
				t.Errorf("child value = %d, want %d", got, want)
			}
		})
	}
}

// A single custom value takes the lazy, slot-addressable fallback rather than
// the generated ordered path. Its enclosing field has a different saved type.
func TestCapturedSingleCustomField(t *testing.T) {
	original := innerFieldValue{v: 1234}
	var legacy, captured bytes.Buffer
	if _, err := state.Save(t.Context(), &legacy, &original); err != nil {
		t.Fatal(err)
	}
	if _, err := state.SaveCaptured(t.Context(), &captured, &original); err != nil {
		t.Fatal(err)
	}
	if got, want := captured.Bytes(), legacy.Bytes(); !bytes.Equal(got, want) {
		t.Fatalf("custom captured bytes = %x, want legacy %x", got, want)
	}
	var loaded innerFieldValue
	if _, err := state.Load(t.Context(), &captured, &loaded); err != nil {
		t.Fatal(err)
	}
	if got, want := loaded.v, original.v; got != want {
		t.Errorf("custom value = %d, want %d", got, want)
	}
}
