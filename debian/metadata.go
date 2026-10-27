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

// Binary metadata generates the version and changelog of the Debian package.
package main

import (
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"time"
)

const changelog = `runsc (%s) unstable; urgency=medium

  * See https://gvisor.dev/docs/ for release details.

 -- The gVisor Authors <gvisor-dev@googlegroups.com>  %s
`

func readFile(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		log.Fatal(err)
	}
	return string(b)
}

func main() {
	const usage = "usage: metadata version STATUS OUTPUT | changelog VERSION EPOCH_FILE OUTPUT"
	args := os.Args[1:]
	var output string
	switch {
	case len(args) == 3 && args[0] == "version":
		for line := range strings.SplitSeq(readFile(args[1]), "\n") {
			if label, ok := strings.CutPrefix(line, "STABLE_VERSION "); ok {
				// Debian requires a leading digit; Buildkite's test tag has none.
				v := strings.TrimLeftFunc(label, func(c rune) bool { return c < '0' || c > '9' })
				if v == "" {
					v = "0.0.0"
				}
				output = v + "\n"
				break
			}
		}
		if output == "" {
			log.Fatal("missing STABLE_VERSION in workspace status")
		}
	case len(args) == 4 && args[0] == "changelog":
		epoch, err := strconv.ParseUint(readFile(args[2]), 10, 63)
		if err != nil {
			log.Fatalf("invalid SOURCE_DATE_EPOCH: %v", err)
		}
		date := time.Unix(int64(epoch), 0).UTC().Format(time.RFC1123Z)
		output = fmt.Sprintf(changelog, strings.TrimSuffix(readFile(args[1]), "\n"), date)
	default:
		log.Fatal(usage)
	}
	if err := os.WriteFile(args[len(args)-1], []byte(output), 0644); err != nil {
		log.Fatal(err)
	}
}
