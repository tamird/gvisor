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

type pointerGlobalState struct {
	mu sync.Mutex
}

var pointerGlobalMutex = new(sync.Mutex)
var pointerGlobalStruct = &pointerGlobalState{}
var interfaceGlobalMutex sync.Locker = new(sync.Mutex)

var (
	// +checklocks:pointerGlobalMutex
	pointerMutexValue int
	// +checklocks:pointerGlobalStruct.mu
	pointerStructValue int
	// +checklocks:interfaceGlobalMutex
	interfaceMutexValue int
)

// +checklocks:pointerGlobalMutex
func requireGlobalMutex() { pointerMutexValue++ }

// +checklocksexclude:pointerGlobalMutex
func excludeGlobalMutex() {}

// +checklocksacquire:pointerGlobalMutex
func acquireGlobalMutex() { pointerGlobalMutex.Lock() }

// +checklocksrelease:pointerGlobalMutex
func releaseGlobalMutex() { pointerGlobalMutex.Unlock() }

func testGlobalMutexContracts() {
	pointerMutexValue = 1 // +checklocksfail=invalid field access
	requireGlobalMutex()  // +checklocksfail=must hold pointerGlobalMutex
	excludeGlobalMutex()
	acquireGlobalMutex()
	pointerMutexValue = 1
	requireGlobalMutex()
	excludeGlobalMutex() // +checklocksfail=must not hold pointerGlobalMutex
	releaseGlobalMutex()
	requireGlobalMutex() // +checklocksfail=must hold pointerGlobalMutex
	excludeGlobalMutex()
}

// +checklocks:pointerGlobalStruct.mu
func requireGlobalStruct() { pointerStructValue++ }

// +checklocksexclude:pointerGlobalStruct.mu
func excludeGlobalStruct() {}

// +checklocksacquire:pointerGlobalStruct.mu
func acquireGlobalStruct() { pointerGlobalStruct.mu.Lock() }

// +checklocksrelease:pointerGlobalStruct.mu
func releaseGlobalStruct() { pointerGlobalStruct.mu.Unlock() }

func testGlobalStructContracts() {
	pointerStructValue = 1 // +checklocksfail=invalid field access
	requireGlobalStruct()  // +checklocksfail=must hold pointerGlobalStruct.mu
	excludeGlobalStruct()
	acquireGlobalStruct()
	pointerStructValue = 1
	requireGlobalStruct()
	excludeGlobalStruct() // +checklocksfail=must not hold pointerGlobalStruct.mu
	releaseGlobalStruct()
	requireGlobalStruct() // +checklocksfail=must hold pointerGlobalStruct.mu
	excludeGlobalStruct()
}

// +checklocks:interfaceGlobalMutex
func requireGlobalInterface() { interfaceMutexValue++ }

// +checklocksexclude:interfaceGlobalMutex
func excludeGlobalInterface() {}

// +checklocksacquire:interfaceGlobalMutex
func acquireGlobalInterface() { interfaceGlobalMutex.Lock() }

// +checklocksrelease:interfaceGlobalMutex
func releaseGlobalInterface() { interfaceGlobalMutex.Unlock() }

func testGlobalInterfaceContracts() {
	interfaceMutexValue = 1  // +checklocksfail=invalid field access
	requireGlobalInterface() // +checklocksfail=must hold interfaceGlobalMutex
	excludeGlobalInterface()
	acquireGlobalInterface()
	interfaceMutexValue = 1
	requireGlobalInterface()
	excludeGlobalInterface() // +checklocksfail=must not hold interfaceGlobalMutex
	releaseGlobalInterface()
	requireGlobalInterface() // +checklocksfail=must hold interfaceGlobalMutex
	excludeGlobalInterface()
}

func testGlobalPointerAssignment(p *sync.Mutex) {
	pointerGlobalMutex = p
	pointerGlobalMutex.Lock()
	requireGlobalMutex()
	p.Unlock()
}

func testGlobalStructAssignment(p *pointerGlobalState) {
	pointerGlobalStruct = p
	p.mu.Lock()
	requireGlobalStruct()
	pointerGlobalStruct.mu.Unlock()
}

func testGlobalInterfaceAssignment(p sync.Locker) {
	interfaceGlobalMutex = p
	p.Lock()
	requireGlobalInterface()
	interfaceGlobalMutex.Unlock()
}

func testGlobalGuardFollowsReassignment(p, q *sync.Mutex) {
	pointerGlobalMutex = p
	p.Lock()
	pointerGlobalMutex = q
	requireGlobalMutex() // +checklocksfail=must hold pointerGlobalMutex
	q.Lock()
	requireGlobalMutex()
	q.Unlock()
	p.Unlock()
}

func testGlobalInterfaceBoxing(p, q *sync.Mutex) {
	interfaceGlobalMutex = p
	p.Lock()
	requireGlobalInterface()
	interfaceGlobalMutex = q
	requireGlobalInterface() // +checklocksfail=must hold interfaceGlobalMutex
	q.Lock()
	requireGlobalInterface()
	q.Unlock()
	p.Unlock()
}

type extendedGlobalLocker interface {
	sync.Locker
	Extra()
}

func testGlobalInterfaceConversion(p extendedGlobalLocker) {
	interfaceGlobalMutex = p
	p.Lock()
	requireGlobalInterface()
	p.Unlock()
}

func testExportedGlobalMutex() {
	crosspkg.RequireExportedMutex() // +checklocksfail=must hold
	crosspkg.ExcludeExportedMutex()
	crosspkg.ExportedPointerMutex.Lock()
	crosspkg.RequireExportedMutex()
	crosspkg.ExcludeExportedMutex() // +checklocksfail=must not hold
	crosspkg.ExportedPointerMutex.Unlock()
	crosspkg.RequireExportedMutex() // +checklocksfail=must hold
}

func testImportedPrivateGlobalMutex() {
	crosspkg.RequirePrivateMutex() // +checklocksfail=must hold
	crosspkg.ExcludePrivateMutex()
	crosspkg.AcquirePrivateMutex()
	crosspkg.RequirePrivateMutex()
	crosspkg.ExcludePrivateMutex() // +checklocksfail=must not hold
	crosspkg.ReleasePrivateMutex()
	crosspkg.RequirePrivateMutex() // +checklocksfail=must hold
	crosspkg.ExcludePrivateMutex()
}

func testExportedGlobalStruct() {
	crosspkg.RequireExportedStruct() // +checklocksfail=must hold
	crosspkg.ExcludeExportedStruct()
	crosspkg.ExportedPointerStruct.Mu.Lock()
	crosspkg.RequireExportedStruct()
	crosspkg.ExcludeExportedStruct() // +checklocksfail=must not hold
	crosspkg.ExportedPointerStruct.Mu.Unlock()
	crosspkg.RequireExportedStruct() // +checklocksfail=must hold
}

func testImportedPrivateGlobalStruct() {
	crosspkg.RequirePrivatePointerStruct() // +checklocksfail=must hold
	crosspkg.ExcludePrivatePointerStruct()
	crosspkg.AcquirePrivatePointerStruct()
	crosspkg.RequirePrivatePointerStruct()
	crosspkg.ExcludePrivatePointerStruct() // +checklocksfail=must not hold
	crosspkg.ReleasePrivatePointerStruct()
	crosspkg.RequirePrivatePointerStruct() // +checklocksfail=must hold
	crosspkg.ExcludePrivatePointerStruct()
}

func testExportedGlobalInterface() {
	crosspkg.RequireExportedInterface() // +checklocksfail=must hold
	crosspkg.ExcludeExportedInterface()
	crosspkg.ExportedInterfaceMutex.Lock()
	crosspkg.RequireExportedInterface()
	crosspkg.ExcludeExportedInterface() // +checklocksfail=must not hold
	crosspkg.ExportedInterfaceMutex.Unlock()
	crosspkg.RequireExportedInterface() // +checklocksfail=must hold
}

func testImportedPrivateGlobalInterface() {
	crosspkg.RequirePrivateInterface() // +checklocksfail=must hold
	crosspkg.ExcludePrivateInterface()
	crosspkg.AcquirePrivateInterface()
	crosspkg.RequirePrivateInterface()
	crosspkg.ExcludePrivateInterface() // +checklocksfail=must not hold
	crosspkg.ReleasePrivateInterface()
	crosspkg.RequirePrivateInterface() // +checklocksfail=must hold
	crosspkg.ExcludePrivateInterface()
}
