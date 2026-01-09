// Copyright 2019 The gVisor Authors.
//
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file or at
// https://developers.google.com/open-source/licenses/bsd.

// Package atomicptr provides atomic pointers to values.
package atomicptr

import (
	"context"
	"sync/atomic"
)

// An AtomicPtr is a pointer to a value of type Value that can be atomically
// loaded and stored. The zero value of an AtomicPtr represents nil.
//
// +stateify savable
type AtomicPtr[Value any] struct {
	ptr atomic.Pointer[Value] `state:".(*Value)"`
}

func (p *AtomicPtr[Value]) savePtr() *Value {
	return p.Load()
}

func (p *AtomicPtr[Value]) loadPtr(_ context.Context, v *Value) {
	p.Store(v)
}

// Load returns the value set by the most recent Store. It returns nil if there
// has been no previous call to Store.
//
//go:nosplit
func (p *AtomicPtr[Value]) Load() *Value {
	return p.ptr.Load()
}

// Store sets the value returned by Load to x.
func (p *AtomicPtr[Value]) Store(x *Value) {
	p.ptr.Store(x)
}

// Swap atomically stores `x` into *p and returns the previous *p value.
func (p *AtomicPtr[Value]) Swap(x *Value) *Value {
	return p.ptr.Swap(x)
}
