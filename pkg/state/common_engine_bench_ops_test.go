// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package state

// These flat tables supply Go storage access only. The measured engine owns
// the same wire, graph, wait and retained-cell decisions for every adapter.
var commonEngineBenchmarkIntOps valueOperations
var commonEngineBenchmarkMapOps valueOperations
var commonEngineBenchmarkSliceOps valueOperations
var commonEngineBenchmarkBackingOps valueOperations

func (*commonEngineBenchmarkInt) stateValueOperations() *valueOperations {
	return &commonEngineBenchmarkIntOps
}
func (*commonEngineBenchmarkMap) stateValueOperations() *valueOperations {
	return &commonEngineBenchmarkMapOps
}
func (*commonEngineBenchmarkSlice) stateValueOperations() *valueOperations {
	return &commonEngineBenchmarkSliceOps
}

func init() {
	commonEngineBenchmarkIntOps = integerOperations[commonEngineBenchmarkInt]()
	commonEngineBenchmarkMapOps = mapOperations[commonEngineBenchmarkMap](
		func() *valueOperations { return &commonEngineBenchmarkIntOps },
		func() *valueOperations { return &commonEngineBenchmarkSliceOps })
	commonEngineBenchmarkSliceOps = sliceOperations[commonEngineBenchmarkSlice](
		func() *valueOperations { return &commonEngineBenchmarkMapOps },
		func() *valueOperations { return &commonEngineBenchmarkBackingOps })
	commonEngineBenchmarkBackingOps = backingOperations[commonEngineBenchmarkMap](
		func() *valueOperations { return &commonEngineBenchmarkMapOps })
}
