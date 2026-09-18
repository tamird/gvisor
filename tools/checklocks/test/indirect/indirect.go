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

// Package indirect tests lock facts from a package absent from the caller's SSA.
package indirect

import "sync"

var globalMu sync.Mutex

// State exposes data and operations guarded by a private global.
type State struct {
	// +checklocks:globalMu
	Value int
}

// +checklocksacquire:globalMu
//
//go:noinline
func (*State) LockPrivate() { globalMu.Lock() }

// +checklocksrelease:globalMu
//
//go:noinline
func (*State) UnlockPrivate() { globalMu.Unlock() }

// +checklocks:globalMu
func (*State) RequirePrivate() {}

// +checklocksexclude:globalMu
func (*State) ExcludePrivate() {}
