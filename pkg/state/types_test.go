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

package state

import (
	"bytes"
	"context"
	"errors"
	"testing"
)

func init() {
	registerBinary((*binaryFailure)(nil))
	registerBinary((*binaryAppendFailure)(nil))
}

var errBinary = errors.New("binary codec failed")

type binaryFailure struct {
	failMarshal bool
}

func (b *binaryFailure) MarshalBinary() ([]byte, error) {
	if b.failMarshal {
		return nil, errBinary
	}
	return nil, nil
}

func (*binaryFailure) UnmarshalBinary([]byte) error { return errBinary }

// SaverLoader without Type metadata must not override binary registration.
func (*binaryFailure) StateSave(Sink) { panic("unexpected StateSave") }

func (*binaryFailure) StateLoad(context.Context, Source) { panic("unexpected StateLoad") }

type binaryAppendFailure struct {
	binaryFailure
}

func (b *binaryAppendFailure) AppendBinary(data []byte) ([]byte, error) {
	value, err := b.MarshalBinary()
	return append(data, value...), err
}

func TestBinaryErrors(t *testing.T) {
	for name, value := range map[string]func(bool) binaryObject{
		"marshal": func(fail bool) binaryObject { return &binaryFailure{failMarshal: fail} },
		"append": func(fail bool) binaryObject {
			return &binaryAppendFailure{binaryFailure{failMarshal: fail}}
		},
	} {
		t.Run(name, func(t *testing.T) {
			var buf bytes.Buffer
			if _, err := Save(t.Context(), &buf, value(true)); !errors.Is(err, errBinary) {
				t.Fatalf("Save = %v, want %v", err, errBinary)
			}
			buf.Reset()
			if _, err := Save(t.Context(), &buf, value(false)); err != nil {
				t.Fatalf("Save: %v", err)
			}
			if _, err := Load(t.Context(), &buf, value(false)); !errors.Is(err, errBinary) {
				t.Fatalf("Load = %v, want %v", err, errBinary)
			}
		})
	}
}
