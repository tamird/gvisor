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

package tests

import (
	"bytes"
	"errors"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/state"
)

func init() {
	state.Register((*binaryFailure)(nil))
}

func TestBinaryValues(t *testing.T) {
	timestamp := time.Date(2026, time.October, 7, 12, 0, 0, 1, time.UTC)
	values := []any{time.Time{}, timestamp, map[time.Time]int{{}: 1, timestamp: 2}, inner{42}}
	runTestCases(t, false, "values", values)
	runTestCases(t, false, "interfaces", interfacesTo(values))
}

var errBinary = errors.New("binary codec failed")

// Existing state methods must take precedence over binary methods.
func (*inner) MarshalBinary() ([]byte, error) { return nil, errBinary }

func (*inner) UnmarshalBinary([]byte) error { return errBinary }

type binaryFailure struct {
	failSave bool
}

func (b *binaryFailure) MarshalBinary() ([]byte, error) {
	if b.failSave {
		return nil, errBinary
	}
	return nil, nil
}

func (*binaryFailure) UnmarshalBinary([]byte) error { return errBinary }

func TestBinaryErrors(t *testing.T) {
	var buf bytes.Buffer
	if _, err := state.Save(t.Context(), &buf, &binaryFailure{failSave: true}); !errors.Is(err, errBinary) {
		t.Fatalf("Save = %v, want %v", err, errBinary)
	}
	buf.Reset()
	if _, err := state.Save(t.Context(), &buf, &binaryFailure{}); err != nil {
		t.Fatalf("Save: %v", err)
	}
	if _, err := state.Load(t.Context(), &buf, &binaryFailure{}); !errors.Is(err, errBinary) {
		t.Fatalf("Load = %v, want %v", err, errBinary)
	}
}

func TestBinaryTime(t *testing.T) {
	for name, timestamp := range map[string]time.Time{
		"zero":      {},
		"utc":       time.Date(2026, time.October, 7, 12, 0, 0, 1, time.UTC),
		"zone":      time.Date(2026, time.October, 7, 12, 0, 0, 1, time.FixedZone("offset", 3661)),
		"monotonic": time.Now(),
	} {
		t.Run(name, func(t *testing.T) {
			original := timeContainer{timestamp: timestamp}
			original.pointer = &original.timestamp
			var buf bytes.Buffer
			if _, err := state.Save(t.Context(), &buf, &original); err != nil {
				t.Fatalf("Save: %v", err)
			}
			var restored timeContainer
			if _, err := state.Load(t.Context(), &buf, &restored); err != nil {
				t.Fatalf("Load: %v", err)
			}
			if !restored.timestamp.Equal(timestamp) {
				t.Errorf("timestamp = %v, want %v", restored.timestamp, timestamp)
			}
			_, wantOffset := timestamp.Zone()
			if _, gotOffset := restored.timestamp.Zone(); gotOffset != wantOffset {
				t.Errorf("zone offset = %d, want %d", gotOffset, wantOffset)
			}
			// Like the previous UnixNano conversion, binary time encoding
			// omits the process-local monotonic clock reading.
			if restored.timestamp != restored.timestamp.Round(0) {
				t.Errorf("restored timestamp contains a monotonic reading: %v", restored.timestamp)
			}
			if restored.pointer != &restored.timestamp {
				t.Errorf("pointer = %p, want alias %p", restored.pointer, &restored.timestamp)
			}
		})
	}
}
