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

package licensecheck

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/mod/module"
)

// enumerateModuleProxy materializes the resolver-owned inventory through the
// same build and cquery path adapter used by other Make artifact consumers.
func enumerateModuleProxy(name, ref string) ([]dep, error) {
	out, err := makeBazel("build", "--remote_download_outputs=toplevel", ref+"//:license_inventory")
	if err != nil {
		return nil, err
	}
	paths := strings.Split(strings.TrimSpace(out), "\n")
	if len(paths) != 1 || filepath.Base(paths[0]) != "license-inventory.json" {
		return nil, fmt.Errorf("module proxy %s: expected one license inventory output, got %q", name, out)
	}
	f, err := os.Open(paths[0])
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return moduleProxyDependencies(name, f)
}

// listedModule is the supported subset of go list -m's JSON API. The original
// Path identifies a selected dependency; Replace identifies its actual source.
type listedModule struct {
	Path, Version string
	Main          bool
	Replace       *listedModule
	Error         *struct{ Err string }
}

func moduleProxyDependencies(repo string, input io.Reader) ([]dep, error) {
	decoder := json.NewDecoder(input)
	seen := make(map[string]struct{})
	mainCount := 0
	var deps []dep
	for {
		var m listedModule
		if err := decoder.Decode(&m); err == io.EOF {
			break
		} else if err != nil {
			return nil, fmt.Errorf("module proxy %s: invalid inventory: %w", repo, err)
		}
		if m.Error != nil {
			return nil, fmt.Errorf("module proxy %s: resolving %s: %s", repo, m.Path, m.Error.Err)
		}
		_, duplicate := seen[m.Path]
		if m.Path == "" || duplicate {
			return nil, fmt.Errorf("module proxy %s: missing or duplicate module path %q", repo, m.Path)
		}
		seen[m.Path] = struct{}{}
		if m.Main {
			if m.Version != "" || m.Replace != nil {
				return nil, fmt.Errorf("module proxy %s: invalid main module %s", repo, m.Path)
			}
			mainCount++
			continue // The declared main module is source, not a downloaded dependency.
		}
		if err := module.Check(m.Path, m.Version); err != nil {
			return nil, fmt.Errorf("module proxy %s: invalid selected module: %w", repo, err)
		}
		actual := &m
		if m.Replace != nil {
			actual = m.Replace
			if actual.Error != nil {
				return nil, fmt.Errorf("module proxy %s: replacing %s: %s", repo, m.Path, actual.Error.Err)
			}
			if actual.Version == "" {
				return nil, fmt.Errorf("module proxy %s: local replacement of %s is not an auditable module archive", repo, m.Path)
			}
			if err := module.Check(actual.Path, actual.Version); err != nil {
				return nil, fmt.Errorf("module proxy %s: invalid replacement: %w", repo, err)
			}
		}
		// Each proxy resolves independently of Bzlmod and other proxies. Never
		// collapse its actual version into the main graph's highest version.
		deps = append(deps, dep{
			name:       "module_proxy/" + repo + "/" + m.Path,
			kind:       kindGoModule,
			modulePath: actual.Path,
			version:    actual.Version,
		})
	}
	if mainCount != 1 {
		return nil, fmt.Errorf("module proxy %s: inventory has %d main modules, want one", repo, mainCount)
	}
	return deps, nil
}
