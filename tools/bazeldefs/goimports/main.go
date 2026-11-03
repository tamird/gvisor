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

// Binary goimports formats generated Go files using the declared Bazel Go SDK.
package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"golang.org/x/tools/imports"
)

var (
	goTool = flag.String("go", "", "Go executable for resolving imports")
	src    = flag.String("src", "", "generated source filename for import resolution")
	output = flag.String("output", "", "formatted output file")
)

func run() error {
	// goimports shells out to the Go command to resolve imports. It needs an
	// absolute PATH entry because os/exec rejects relative lookups with ErrDot.
	//
	// See:
	// - https://github.com/golang/tools/blob/ea2f152dc/internal/gocommand/invoke.go
	// - https://github.com/golang/tools/blob/ea2f152dc/internal/imports/fix.go
	// - https://pkg.go.dev/os/exec#hdr-Executables_in_the_current_directory
	goPath, err := filepath.Abs(*goTool)
	if err != nil {
		return err
	}
	if err := os.Setenv("PATH", filepath.Dir(goPath)); err != nil {
		return err
	}
	generated, err := os.ReadFile(*src)
	if err != nil {
		return err
	}
	formatted, err := imports.Process(*src, generated, nil)
	if err != nil {
		return err
	}
	return os.WriteFile(*output, formatted, 0644)
}

func main() {
	flag.Parse()
	if *goTool == "" || *src == "" || *output == "" {
		flag.Usage()
		os.Exit(1)
	}
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
