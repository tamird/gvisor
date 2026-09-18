// Copyright 2022 The gVisor Authors.
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

// Package crosspkg is a second package for testing.
package crosspkg

import (
	"sync"
)

var (
	// +checklocks:FooMu
	Foo   int
	FooMu sync.Mutex
)

// GenericGuard is a generic type with a guarded field. This is used to verify
// that facts exported by this package are correctly imported when another
// package instantiates GenericGuard[T].
type GenericGuard[T any] struct {
	Mu sync.Mutex
	// +checklocks:Mu
	Value T
}

// Integers delegates to a private constructor. The consumer can only learn
// about the returned closure through facts exported on this public function.
func Integers() func(func(int) bool) {
	return integers()
}

func integers() func(func(int) bool) {
	return func(yield func(int) bool) {
		for value := 0; value < 3; value++ {
			if !yield(value) {
				return
			}
		}
	}
}

// TransientUnlockSeq returns a callable with a lock precondition. An empty
// constructor contract must not be mistaken for a proof about that callable.
func TransientUnlockSeq() func(func(int) bool) {
	return transientUnlockSeq
}

// +checklocks:FooMu
func transientUnlockSeq(yield func(int) bool) {
	FooMu.Unlock()
	yield(1)
	FooMu.Lock()
}
