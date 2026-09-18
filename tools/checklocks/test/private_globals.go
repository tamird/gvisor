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

package test

import (
	"sync"

	"gvisor.dev/gvisor/tools/checklocks/test/crosspkg"
)

func testPrivateGlobal(v *crosspkg.PrivateState) {
	crosspkg.RequirePrivate() // +checklocksfail=must hold globalMu
	crosspkg.PrivateValue = 1 // +checklocksfail=invalid field access
	crosspkg.ExcludePrivate()
	crosspkg.LockPrivate()
	crosspkg.RequirePrivate()
	crosspkg.PrivateValue = 1
	crosspkg.ExcludePrivate() // +checklocksfail=must not hold globalMu
	crosspkg.UnlockPrivate()
	crosspkg.RequirePrivate() // +checklocksfail=must hold globalMu
	crosspkg.ExcludePrivate()

	crosspkg.RequirePrivateStruct() // +checklocksfail=must hold globalStruct.mu
	v.Value = 1                     // +checklocksfail=invalid field access
	crosspkg.ExcludePrivateStruct()
	crosspkg.LockPrivateStruct()
	crosspkg.RequirePrivateStruct()
	v.Value = 1
	crosspkg.ExcludePrivateStruct() // +checklocksfail=must not hold globalStruct.mu
	crosspkg.UnlockPrivateStruct()
	crosspkg.RequirePrivateStruct() // +checklocksfail=must hold globalStruct.mu
	crosspkg.ExcludePrivateStruct()
}

func testPrivateGlobalNamesArePackageQualified(v *crosspkg.IndirectState) {
	globalMu.Lock()
	crosspkg.RequirePrivate() // +checklocksfail=must hold globalMu
	crosspkg.ExcludePrivate()
	globalMu.Unlock()

	globalStruct.mu.Lock()
	crosspkg.RequirePrivateStruct() // +checklocksfail=must hold globalStruct.mu
	crosspkg.ExcludePrivateStruct()
	globalStruct.mu.Unlock()

	crosspkg.LockPrivate()
	v.RequirePrivate() // +checklocksfail=must hold globalMu
	v.ExcludePrivate()
	crosspkg.UnlockPrivate()
}

var FooMu sync.Mutex

func testExportedGlobalNamesArePackageQualified() {
	FooMu.Lock()
	crosspkg.Foo = 1 // +checklocksfail=invalid field access
	FooMu.Unlock()
}

func testIndirectPrivateGlobal(v *crosspkg.IndirectState) {
	v.RequirePrivate() // +checklocksfail=must hold globalMu
	v.Value = 1        // +checklocksfail=invalid field access
	v.ExcludePrivate()
	v.LockPrivate()
	v.RequirePrivate()
	v.Value = 1
	v.ExcludePrivate() // +checklocksfail=must not hold globalMu
	v.UnlockPrivate()
	v.RequirePrivate() // +checklocksfail=must hold globalMu
	v.ExcludePrivate()
}

type invalidGlobalMutex = sync.Mutex

// +checklocks:invalidGlobalMutex
func testTypeCannotBeGlobalGuard() {} // +checklocksfail=does not have a match
