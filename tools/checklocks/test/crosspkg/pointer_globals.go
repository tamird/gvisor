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

package crosspkg

import "sync"

// PointerGlobalState holds a mutex accessed through a package-level pointer.
type PointerGlobalState struct {
	Mu sync.Mutex
}

var ExportedPointerMutex = new(sync.Mutex)
var ExportedPointerStruct = &PointerGlobalState{}
var ExportedInterfaceMutex sync.Locker = new(sync.Mutex)

var privatePointerMutex = new(sync.Mutex)
var privatePointerStruct = &PointerGlobalState{}
var privateInterfaceMutex sync.Locker = new(sync.Mutex)

// +checklocks:ExportedPointerMutex
func RequireExportedMutex() {}

// +checklocksexclude:ExportedPointerMutex
func ExcludeExportedMutex() {}

// +checklocks:privatePointerMutex
func RequirePrivateMutex() {}

// +checklocksexclude:privatePointerMutex
func ExcludePrivateMutex() {}

// +checklocksacquire:privatePointerMutex
//
//go:noinline
func AcquirePrivateMutex() { privatePointerMutex.Lock() }

// +checklocksrelease:privatePointerMutex
//
//go:noinline
func ReleasePrivateMutex() { privatePointerMutex.Unlock() }

// +checklocks:ExportedPointerStruct.Mu
func RequireExportedStruct() {}

// +checklocksexclude:ExportedPointerStruct.Mu
func ExcludeExportedStruct() {}

// +checklocks:privatePointerStruct.Mu
func RequirePrivatePointerStruct() {}

// +checklocksexclude:privatePointerStruct.Mu
func ExcludePrivatePointerStruct() {}

// +checklocksacquire:privatePointerStruct.Mu
//
//go:noinline
func AcquirePrivatePointerStruct() { privatePointerStruct.Mu.Lock() }

// +checklocksrelease:privatePointerStruct.Mu
//
//go:noinline
func ReleasePrivatePointerStruct() { privatePointerStruct.Mu.Unlock() }

// +checklocks:ExportedInterfaceMutex
func RequireExportedInterface() {}

// +checklocksexclude:ExportedInterfaceMutex
func ExcludeExportedInterface() {}

// +checklocks:privateInterfaceMutex
func RequirePrivateInterface() {}

// +checklocksexclude:privateInterfaceMutex
func ExcludePrivateInterface() {}

// +checklocksacquire:privateInterfaceMutex
//
//go:noinline
func AcquirePrivateInterface() { privateInterfaceMutex.Lock() }

// +checklocksrelease:privateInterfaceMutex
//
//go:noinline
func ReleasePrivateInterface() { privateInterfaceMutex.Unlock() }
