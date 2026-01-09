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

// Package test is a test package.
package test

import (
	"context"

	"gvisor.dev/gvisor/pkg/state"
)

type box[T any] struct {
	Value *T
}

func (b *box[T]) StateTypeName() string {
	return "test.box"
}

func (b *box[T]) StateFields() []string {
	return []string{"Value"}
}

func (b *box[T]) StateSave(s state.Sink) {
	s.Save(0, &b.Value)
}

func (b *box[T]) StateLoad(ctx context.Context, src state.Source) {
	src.Load(0, &b.Value)
}

// +stateify savable
type plain struct {
	N int
}

// +stateify savable
type root struct {
	b box[int]
	p plain
}

// +stateify savable
type boxedInt = box[int]

// +stateify savable
type genericRoot struct {
	b boxedInt
}
