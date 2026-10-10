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

import "time"

type capturedSigned int16

// captureRecord separates consecutive primitive fields with an inline child
// and repeated pointer, so byte runs cannot reorder or duplicate graph edges.
// +stateify savable
type captureRecord struct {
	enabled   bool
	signed    capturedSigned
	unsigned  uint64
	text      string
	child     inner
	link      *inner
	float32   float32
	complex64 complex64
	elapsed   time.Duration
	again     *inner
}
