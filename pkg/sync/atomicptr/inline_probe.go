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

package atomicptr

// Keep these callers materialized while allowing the reviewed methods to inline.

//go:noinline
//go:nosplit
func loadBefore(p *AtomicPtrBeforeInt) *int { return p.Load() }

//go:noinline
//go:nosplit
func loadAfter(p *AtomicPtrInt) *int { return p.Load() }

//go:noinline
func storeBefore(p *AtomicPtrBeforeInt, value *int) { p.Store(value) }

//go:noinline
func storeAfter(p *AtomicPtrInt, value *int) { p.Store(value) }

//go:noinline
func swapBefore(p *AtomicPtrBeforeInt, value *int) *int { return p.Swap(value) }

//go:noinline
func swapAfter(p *AtomicPtrInt, value *int) *int { return p.Swap(value) }

// This deliberately preserved direct call is the negative inlining control.

//go:noinline
//go:nosplit
func loadNoInline(p *AtomicPtrInt) *int { return p.Load() }

//go:noinline
//go:nosplit
func loadControl(p *AtomicPtrInt) *int { return loadNoInline(p) }
