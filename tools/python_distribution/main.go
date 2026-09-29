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

// Binary python_distribution stages declared sources for the PyPA build frontend.
package main

import (
	"bytes"
	"flag"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/BurntSushi/toml"
)

var (
	pyproject = flag.String("pyproject", "", "Project's pyproject.toml")
	frontend  = flag.String("frontend", "", "Declared PyPA build executable")
	version   = flag.String("version", "", "Optional project version override")
	output    = flag.String("output", "", "Distribution output directory")
)

func build() error {
	command, err := filepath.Abs(*frontend)
	if err != nil {
		return err
	}
	out, err := filepath.Abs(*output)
	if err != nil {
		return err
	}
	work, err := os.MkdirTemp("", "python-distribution-")
	if err != nil {
		return err
	}
	defer func() { _ = os.RemoveAll(work) }()
	project := filepath.Join(work, "project")
	for _, source := range append(flag.Args(), *pyproject) {
		relative, err := filepath.Rel(filepath.Dir(*pyproject), source)
		if err != nil {
			return err
		}
		if !filepath.IsLocal(relative) {
			return fmt.Errorf("source %q is outside project %q", source, *pyproject)
		}
		destination := filepath.Join(project, relative)
		if err := os.MkdirAll(filepath.Dir(destination), 0755); err != nil {
			return err
		}
		contents, err := os.ReadFile(source)
		if err != nil {
			return err
		}
		if err := os.WriteFile(destination, contents, 0644); err != nil {
			return err
		}
	}
	if *version != "" {
		filename := filepath.Join(project, "pyproject.toml")
		var metadata map[string]any
		if _, err := toml.DecodeFile(filename, &metadata); err != nil {
			return err
		}
		projectMetadata, ok := metadata["project"].(map[string]any)
		if !ok {
			return fmt.Errorf("%s has no project table", filename)
		}
		// The backend validates and normalizes the version. Preserve all other
		// metadata, including fields this staging tool does not know about.
		projectMetadata["version"] = *version
		var contents bytes.Buffer
		if err := toml.NewEncoder(&contents).Encode(metadata); err != nil {
			return err
		}
		if err := os.WriteFile(filename, contents.Bytes(), 0644); err != nil {
			return err
		}
	}
	cmd := exec.Command(command, "--no-isolation", "--outdir", out, project)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	cmd.Env = append(os.Environ(), "HOME="+work, "PYTHONNOUSERSITE=1", "PIP_NO_INDEX=1",
		// Wheel's standard timestamp input; no sdist reproducibility claim.
		"SOURCE_DATE_EPOCH=315532800")
	return cmd.Run()
}

func main() {
	flag.Parse()
	if err := build(); err != nil {
		log.Fatal(err)
	}
}
