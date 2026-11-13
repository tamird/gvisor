// Copyright 2024 The gVisor Authors.
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

package parser

import (
	"fmt"
	"os"

	"gvisor.dev/gvisor/pkg/sentry/devices/nvproxy"
)

// CreateIncludeFiles creates the necessary include files for the given driver version, and returns
// the config options for the files.
func CreateIncludeFiles(dir string, driverSource DriverSourceDir, nonUVMIoctls, uvmIoctls []nvproxy.IoctlName, uvmConstants []string) ([]ClangASTConfig, error) {
	// Create include file for non-uvm sources
	nonUVMFile, err := os.CreateTemp(dir, "include_non_uvm_*.cc")
	if err != nil {
		return nil, fmt.Errorf("failed to create temporary file: %w", err)
	}
	defer nonUVMFile.Close()

	includeSources, err := driverSource.GetNonUVMSourcePaths()
	if err != nil {
		return nil, fmt.Errorf("failed to get non-uvm include paths: %w", err)
	}
	if err := WriteIncludeFile(includeSources, nonUVMFile, nonUVMIoctls, nil /* constants */); err != nil {
		return nil, fmt.Errorf("failed to write include file: %w", err)
	}
	configNonUVM := NewParserConfig(
		dir,
		nonUVMFile.Name(),
		driverSource.GetNonUVMIncludePaths(),
	)

	// Create include file for uvm sources
	UVMFile, err := os.CreateTemp(dir, "include_uvm_*.cc")
	if err != nil {
		return nil, fmt.Errorf("failed to create temporary file: %w", err)
	}
	defer UVMFile.Close()

	includeSources = driverSource.GetUVMSourcePaths()
	if err := WriteIncludeFile(includeSources, UVMFile, uvmIoctls, uvmConstants); err != nil {
		return nil, fmt.Errorf("failed to write include file: %w", err)
	}
	configUVM := NewParserConfig(
		dir,
		UVMFile.Name(),
		driverSource.GetUVMIncludePaths(),
	)

	return []ClangASTConfig{configNonUVM, configUVM}, nil
}
