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

// development is a display label, not an assertion that two binaries match.
const development = "(devel)"

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
	return development
}

// CheckpointLabel returns the release label whose binaries may restore one
// another's checkpoints. An empty result identifies a development build.
// Use only --embed_label: a workspace status command can report the same
// STABLE_VERSION for unrelated binaries.
func CheckpointLabel() string {
	switch buildLabel {
	case "0.0.0", "VERSION_MISSING", development:
		// These placeholders can appear in checkpoints with no release identity.
		return ""
	default:
		return buildLabel
	}
}

// Matches reports whether label identifies this build. Two unstamped binaries
// do not establish compatibility merely by having the same missing version.
func Matches(label string) bool {
	switch label {
	case "", development, "VERSION_MISSING":
		return false
	case "0.0.0":
		// A custom status command may explicitly select this legacy label.
		return version == label
	default:
		return label == Version()
	}
}
