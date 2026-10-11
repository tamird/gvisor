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

package wire

// RefArray stores a homogeneous reference array without one Object interface
// and allocation per element. Contents must not move while a graph resolver
// retains element pointers. Each save reads the current reference values.
// Loading uses the ordinary mutable Array representation.
type RefArray struct {
	Contents []Ref
}

func (a *RefArray) save(w *Writer) {
	Uint(len(a.Contents)).save(w)
	if len(a.Contents) == 0 {
		return
	}
	typeRef.save(w)
	for i := range a.Contents {
		a.Contents[i].save(w)
	}
}

func (*RefArray) load(r *Reader) Object {
	a := loadArray(r)
	return &a
}
