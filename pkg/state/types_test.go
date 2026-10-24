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
	"errors"
	"fmt"
	"strings"
	"testing"
)

func TestInvalidArrayPresence(t *testing.T) {
	for _, marker := range []byte{2, 255} {
		t.Run(fmt.Sprint(marker), func(t *testing.T) {
			defer func() {
				if got := recover(); got == nil || !strings.Contains(fmt.Sprint(got), "invalid array presence marker") {
					t.Errorf("DecodeArray panic = %v, want invalid array presence marker", got)
				}
			}()
			var ds decodeState
			ds.r.Reader = bytes.NewReader([]byte{marker, 9})
			d := Decoder{r: &ds.r}
			a := [1]byte{7}
			DecodeArray(&d, a[:], DecodeUint[byte])
		})
	}
}

func init() {
	registerBinary((*binaryFailure)(nil))
}

var errBinary = errors.New("binary codec failed")

type binaryFailure struct {
	failAppend bool
}

func (b *binaryFailure) AppendBinary(data []byte) ([]byte, error) {
	if b.failAppend {
		return nil, errBinary
	}
	return data, nil
}

func (*binaryFailure) UnmarshalBinary([]byte) error { return errBinary }

func TestBinaryErrors(t *testing.T) {
	var buf bytes.Buffer
	if _, err := Save(t.Context(), &buf, &binaryFailure{failAppend: true}); !errors.Is(err, errBinary) {
		t.Fatalf("Save = %v, want %v", err, errBinary)
	}
	buf.Reset()
	if _, err := Save(t.Context(), &buf, &binaryFailure{}); err != nil {
		t.Fatalf("Save: %v", err)
	}
	if _, err := Load(t.Context(), &buf, &binaryFailure{}); !errors.Is(err, errBinary) {
		t.Fatalf("Load = %v, want %v", err, errBinary)
	}
}
