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

// Binary extract writes NVIDIA ABI definitions from declared source inputs.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"

	"gvisor.dev/gvisor/pkg/sentry/devices/nvproxy"
	"gvisor.dev/gvisor/pkg/sentry/devices/nvproxy/nvconf"
	"gvisor.dev/gvisor/tools/nvidia_driver_differ/parser"
)

func extract(parserPath, sourceDir, version, output string, compilerArgs []string) error {
	driverVersion, err := nvconf.DriverVersionFrom(version)
	if err != nil {
		return err
	}
	nvproxy.Init()
	info, ok := nvproxy.SupportedIoctls(driverVersion)
	if !ok {
		return fmt.Errorf("driver %s is not in the nvproxy ABI registry", version)
	}
	runner, err := parser.NewRunner(parserPath)
	if err != nil {
		return err
	}
	defer runner.Cleanup()
	if err := runner.CreateInputFile(info); err != nil {
		return err
	}
	defs, err := runner.ParseSourceDirectory(sourceDir, compilerArgs)
	if err != nil {
		return err
	}
	// Preserve source provenance without embedding a particular worker's
	// absolute execution root in the cached ABI artifact.
	workingDir, err := os.Getwd()
	if err != nil {
		return err
	}
	for name, def := range defs.Records {
		def.Source = strings.TrimPrefix(def.Source, workingDir+string(os.PathSeparator))
		defs.Records[name] = def
	}
	data, err := json.Marshal(defs)
	if err != nil {
		return err
	}
	return os.WriteFile(output, data, 0644)
}

func main() {
	parserPath := flag.String("parser", "", "declared driver_ast_parser executable")
	sourceDir := flag.String("source", "", "declared driver source directory")
	version := flag.String("version", "", "supported NVIDIA driver version")
	output := flag.String("output", "", "output ABI JSON file")
	flag.Parse()
	if *parserPath == "" || *sourceDir == "" || *version == "" || *output == "" || flag.NArg() == 0 {
		fmt.Fprintln(os.Stderr, "require --parser, --source, --version, --output and compiler argv after --")
		flag.Usage()
		os.Exit(1)
	}
	if err := extract(*parserPath, *sourceDir, *version, *output, flag.Args()); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
