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

// Binary package_version writes the Debian version from Bazel's stable status file.
package main

import (
	"log"
	"os"
	"strings"
)

func main() {
	status, err := os.ReadFile(os.Getenv("BAZEL_STABLE_STATUS_FILE"))
	if err != nil {
		log.Fatal(err)
	}
	var label, legacy string
	for line := range strings.SplitSeq(string(status), "\n") {
		key, value, ok := strings.Cut(line, " ")
		if !ok {
			continue
		}
		switch key {
		case "BUILD_EMBED_LABEL":
			label = value
		case "STABLE_VERSION":
			legacy = value
		}
	}
	if legacy != "" {
		label = legacy
	} else if label == "" || label == "0.0.0" || label == "VERSION_MISSING" {
		log.Fatal("missing package version: set --embed_label or supply STABLE_VERSION")
	}
	// A Debian version starts at the first digit in the revision or tag.
	version := strings.TrimLeftFunc(label, func(r rune) bool { return r < '0' || r > '9' })
	if version == "" && legacy == "" {
		log.Fatalf("package version %q must contain a digit", label)
	}
	if err := os.WriteFile(os.Args[1], []byte(version+"\n"), 0o644); err != nil {
		log.Fatal(err)
	}
}
