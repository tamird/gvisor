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
	"archive/tar"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"maps"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
)

// aptLock is the resolver-owned version-2 lock_content exposed by
// translate_dependency_set. Sets contain roots; depends_on contains the
// resolved package edges. The global packages map also includes other hubs.
type aptLock struct {
	Version        int
	DependencySets map[string]struct {
		Sets map[string]map[string]string
	} `json:"dependency_sets"`
	Packages map[string]struct {
		Name, Version, Architecture, SHA256, Filename, Suite string
		DependsOn                                            []string `json:"depends_on"`
	}
	Sources map[string]struct{ URIs []string }
}

func aptDependencies(name, ref string, repo repoInfo) ([]dep, error) {
	var lock aptLock
	if err := json.Unmarshal([]byte(repo.first("lock_content")), &lock); err != nil {
		return nil, fmt.Errorf("apt hub %s: invalid lock_content: %w", name, err)
	}
	if lock.Version != 2 {
		return nil, fmt.Errorf("apt hub %s: unsupported lock version %d", name, lock.Version)
	}
	selected, ok := lock.DependencySets[repo.first("depset_name")]
	if !ok || len(selected.Sets) == 0 {
		return nil, fmt.Errorf("apt hub %s: missing selected dependency set", name)
	}
	byName := make(map[string]dep)
	for arch, roots := range selected.Sets {
		if len(roots) == 0 {
			return nil, fmt.Errorf("apt hub %s: empty %s package set", name, arch)
		}
		var pending []string
		for key, version := range roots {
			pending = append(pending, key+"="+version)
		}
		seen := make(map[string]struct{})
		for len(pending) != 0 {
			key := pending[len(pending)-1]
			pending = pending[:len(pending)-1]
			if _, ok := seen[key]; ok {
				continue
			}
			seen[key] = struct{}{}
			pkg, ok := lock.Packages[key]
			if !ok || (pkg.Architecture != arch && pkg.Architecture != "all") {
				return nil, fmt.Errorf("apt hub %s: missing or wrong-architecture package %s", name, key)
			}
			if !fs.ValidPath(pkg.Name) || pkg.Name == "." || strings.ContainsAny(pkg.Name, `/\`) || pkg.Version == "" ||
				key != "/"+pkg.Suite+"/"+pkg.Name+":"+arch+"="+pkg.Version {
				return nil, fmt.Errorf("apt hub %s: inconsistent package identity %s", name, key)
			}
			hash, err := hex.DecodeString(pkg.SHA256)
			if err != nil || len(hash) != sha256.Size {
				return nil, fmt.Errorf("apt package %s has no valid SHA256 pin", key)
			}
			source := lock.Sources[pkg.Suite]
			if len(source.URIs) == 0 || !fs.ValidPath(pkg.Filename) || strings.Contains(pkg.Filename, `\`) {
				return nil, fmt.Errorf("apt package %s has no valid archive source", key)
			}
			artifact := strings.TrimRight(source.URIs[0], "/") + "/" + pkg.Filename
			u, err := url.Parse(artifact)
			if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
				return nil, fmt.Errorf("apt package %s must have an HTTPS archive URL without credentials", key)
			}
			d := dep{name: "apt/" + name + "/" + arch + "/" + pkg.Name, kind: kindApt,
				url: artifact, sha256: hex.EncodeToString(hash), aptRepo: ref, aptArch: arch, aptPackage: pkg.Name}
			if old, ok := byName[d.name]; ok && old != d {
				return nil, fmt.Errorf("conflicting apt package artifacts for %s", d.name)
			}
			byName[d.name] = d
			for _, dependency := range pkg.DependsOn {
				other, ok := lock.Packages[dependency]
				if !ok {
					return nil, fmt.Errorf("apt package %s has missing dependency %s", key, dependency)
				}
				// Match translate_dependency_set's package_deps_for_architecture;
				// an edge for another CPU is not part of this selected payload.
				if other.Architecture == arch || other.Architecture == "all" {
					pending = append(pending, dependency)
				}
			}
		}
	}
	return slices.Collect(maps.Values(byName)), nil // enumerate sorts all dependencies.
}

// aptFile retains archive contents and links without extracting into the host
// filesystem. Directory links matter: libgcc-s1's entire doc directory points
// to gcc-12-base in the selected Ubuntu package payload.
type aptFile struct {
	text, link string
}

func readAptArchive(input io.Reader) (map[string]aptFile, error) {
	files := make(map[string]aptFile)
	reader := tar.NewReader(input)
	for {
		header, err := reader.Next()
		if err == io.EOF {
			return files, nil
		}
		if err != nil {
			return nil, err
		}
		name := strings.TrimPrefix(header.Name, "./")
		if name == "." && header.Typeflag == tar.TypeDir {
			continue
		}
		name = strings.TrimSuffix(name, "/")
		if !fs.ValidPath(name) || strings.Contains(name, `\`) {
			return nil, fmt.Errorf("invalid apt archive path %q", header.Name)
		}
		// Retain every link, since an ancestor can redirect a copyright path.
		// Other file contents are unnecessary for the license audit.
		var file aptFile
		switch header.Typeflag {
		case tar.TypeDir:
			continue
		case tar.TypeSymlink, tar.TypeLink:
			link := header.Linkname
			if header.Typeflag == tar.TypeSymlink && !strings.HasPrefix(link, "/") {
				link = path.Join(path.Dir(name), link)
			}
			file.link = path.Clean(strings.TrimPrefix(link, "/"))
			if !fs.ValidPath(file.link) || strings.Contains(file.link, `\`) {
				return nil, fmt.Errorf("invalid apt archive link %s -> %s", name, header.Linkname)
			}
		case tar.TypeReg:
			if !strings.HasPrefix(name, "usr/share/doc/") && !strings.HasPrefix(name, "usr/share/common-licenses/") {
				continue
			}
			body, err := io.ReadAll(reader)
			if err != nil {
				return nil, err
			}
			file.text = string(body)
		default:
			continue
		}
		if old, ok := files[name]; ok && old != file {
			return nil, fmt.Errorf("conflicting apt archive member %s", name)
		}
		files[name] = file
	}
}

func aptFileText(files map[string]aptFile, name string) (string, error) {
	resolved, err := resolveAptPath(files, name, make(map[string]struct{}))
	if err != nil {
		return "", err
	}
	file, ok := files[resolved]
	if !ok || file.text == "" {
		return "", fmt.Errorf("missing or empty apt license file %s", resolved)
	}
	return file.text, nil
}

func resolveAptPath(files map[string]aptFile, name string, visiting map[string]struct{}) (string, error) {
	parts := strings.Split(name, "/")
	for i := range parts {
		prefix := strings.Join(parts[:i+1], "/")
		file := files[prefix]
		if file.link == "" {
			continue
		}
		if _, ok := visiting[prefix]; ok {
			return "", fmt.Errorf("apt archive link cycle at %s", prefix)
		}
		visiting[prefix] = struct{}{}
		target, err := resolveAptPath(files, file.link, visiting)
		delete(visiting, prefix)
		if err != nil {
			return "", err
		}
		return resolveAptPath(files, path.Join(append([]string{target}, parts[i+1:]...)...), visiting)
	}
	return name, nil
}

// A sentence-ending period is not part of a common-license basename. Embedded
// periods in names such as LGPL-2.1 remain significant.
var commonLicenseRE = regexp.MustCompile(`/usr/share/common-licenses/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*`)

func aptLicenseText(files map[string]aptFile, pkg string) (string, error) {
	copyright, err := aptFileText(files, "usr/share/doc/"+pkg+"/copyright")
	if err != nil {
		return "", err
	}
	texts := []string{copyright} // Preserve notices, grants and exceptions verbatim.
	refs := commonLicenseRE.FindAllString(copyright, -1)
	slices.Sort(refs)
	for _, ref := range slices.Compact(refs) {
		text, err := aptFileText(files, strings.TrimPrefix(ref, "/"))
		if err != nil {
			return "", fmt.Errorf("%s copyright references %s: %w", pkg, ref, err)
		}
		texts = append(texts, text)
	}
	return strings.Join(texts, "\n\n"), nil
}

// fetchAptLicenses uses the hub's public :flat target, preserving the supplier's
// package extraction and architecture selection. Each archive is built/read
// once, not once for every package in its closure.
func fetchAptLicenses(deps []dep) map[string]fetchResult {
	type selection struct{ repo, arch string }
	groups := make(map[selection][]dep)
	var order []selection
	for _, d := range deps {
		if d.kind == kindApt {
			key := selection{d.aptRepo, d.aptArch}
			if _, ok := groups[key]; !ok {
				order = append(order, key)
			}
			groups[key] = append(groups[key], d)
		}
	}
	results := make(map[string]fetchResult)
	for _, selected := range order {
		files, err := materializeAptArchive(selected.repo, selected.arch)
		for _, d := range groups[selected] {
			if err != nil {
				results[d.name] = fetchResult{err: err}
				continue
			}
			text, textErr := aptLicenseText(files, d.aptPackage)
			var licenses Licenses
			if textErr == nil {
				licenses, textErr = classify(text)
			}
			results[d.name] = fetchResult{fetched: &fetched{sha256: d.sha256, license: licenses}, err: textErr}
		}
	}
	return results
}

func materializeAptArchive(repo, arch string) (map[string]aptFile, error) {
	config, ok := map[string]string{"amd64": "x86_64", "arm64": "aarch64"}[arch]
	if !ok {
		return nil, fmt.Errorf("apt hub %s: unsupported target architecture %s", repo, arch)
	}
	out, err := makeBazel("build", "--remote_download_outputs=toplevel --config="+config, repo+"//:flat")
	if err != nil {
		return nil, err
	}
	paths := strings.Split(strings.TrimSpace(out), "\n")
	if len(paths) != 1 || filepath.Base(paths[0]) != "flat.tar" {
		return nil, fmt.Errorf("apt hub %s: expected one flat.tar output, got %q", repo, out)
	}
	f, err := os.Open(paths[0])
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return readAptArchive(f)
}
