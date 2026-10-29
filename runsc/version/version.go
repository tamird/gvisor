// Copyright 2023 The gVisor Authors.
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

//go:build !false
// +build !false

// Package version holds a string containing version information for runsc.
// Other packages may import it to get this information while avoiding
// import loops.
package version

const missing = "VERSION_MISSING"

// The linker sets buildLabel from Bazel's --embed_label and version from
// STABLE_VERSION for callers using their own workspace status command.
var buildLabel, version string

// Version returns the version string.
func Version() string {
	if version != "" {
		return version
	}
	if buildLabel != "" {
		return buildLabel
	}
	return missing
}

// Matches reports whether label identifies this build. Two unstamped binaries
// do not establish compatibility merely by having the same missing version.
func Matches(label string) bool {
	switch label {
	case "", missing:
		return false
	case "0.0.0":
		// Preserve the historical fallback outside a Git checkout.
		return version == label
	default:
		return label == Version()
	}
}
