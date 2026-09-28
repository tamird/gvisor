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

// configure_runtime preserves Make's installed-runtime interface while sharing
// the private Docker test daemon's runtime variants.
package main

import (
	"flag"
	"fmt"
	"log"
	"os"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
)

func main() {
	// Imported test helpers register their own flags on flag.CommandLine.
	flags := flag.NewFlagSet(os.Args[0], flag.ExitOnError)
	runsc := flags.String("runsc", "", "runtime executable selected by Make")
	name := flags.String("name", "", "base runtime name")
	configPath := flags.String("config", "", "Docker daemon configuration to update")
	suite := flags.String("suite", "docker", "declared runtime configuration set")
	variant := flags.String("variant", "", "install only this configuration under --name")
	listVariants := flags.Bool("list-variants", false, "list the suite's configuration names without installing")
	flags.Parse(os.Args[1:])
	variants, err := dockerutil.RuntimeVariants(*suite)
	if err != nil {
		log.Fatal(err)
	}
	if *listVariants {
		for _, variant := range variants {
			fmt.Println(variant.Name)
		}
		return
	}
	if *variant != "" {
		var selected []dockerutil.RuntimeVariant
		for _, v := range variants {
			if v.Name == *variant {
				selected = []dockerutil.RuntimeVariant{{Args: v.Args}}
				break
			}
		}
		if selected == nil {
			log.Fatalf("unknown %s runtime configuration %q", *suite, *variant)
		}
		variants = selected
	} else if *suite != "docker" {
		log.Fatal("--variant is required for this suite")
	}
	if *runsc == "" || *name == "" || *configPath == "" {
		log.Fatal("--runsc, --name, and --config are required")
	}
	if err := dockerutil.InstallRuntimeVariants(*runsc, *name, *configPath, flags.Args(), variants); err != nil {
		log.Fatal(err)
	}
}
