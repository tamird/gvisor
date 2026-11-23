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
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
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

// The image and auditor consume the same standard checksum manifest. The
// archive filename carries the immutable Git commit.
type nodeSource struct {
	image, commit, sha256 string
}

var nodeSourceRE = regexp.MustCompile(`^([0-9a-f]{64})  node-([0-9a-f]{40})\.tar\.gz\n?$`)

func nodeArchiveSource(image string, manifest []byte) (nodeSource, error) {
	match := nodeSourceRE.FindSubmatch(manifest)
	if match == nil {
		return nodeSource{}, fmt.Errorf("%s: expected one SHA256 row for node-<full Git commit>.tar.gz", image)
	}
	return nodeSource{image: image, commit: string(match[2]), sha256: string(match[1])}, nil
}

func enumerateNodeNpm() ([]dep, error) {
	// A source manifest is also consumed by its image's Dockerfile. Legacy
	// images without this declaration do not introduce a new npm audit input.
	files, err := filepath.Glob("images/runtimes/nodejs*/source.sha256")
	if err != nil {
		return nil, err
	}
	var deps []dep
	for _, file := range files {
		image := filepath.Base(filepath.Dir(file))
		manifest, err := os.ReadFile(file)
		if err != nil {
			return nil, err
		}
		source, err := nodeArchiveSource(image, manifest)
		if err != nil {
			return nil, err
		}
		body, err := httpGet("https://codeload.github.com/nodejs/node/tar.gz/"+source.commit, nil)
		if err != nil {
			return nil, err
		}
		cohorts, err := nodeNpmLocks(source, body)
		if err != nil {
			return nil, err
		}
		deps = append(deps, cohorts...)
	}
	return deps, nil
}

func nodeNpmLocks(source nodeSource, body []byte) ([]dep, error) {
	if sum := sha256.Sum256(body); hex.EncodeToString(sum[:]) != source.sha256 {
		return nil, fmt.Errorf("%s: Node archive SHA256 mismatch: got %x, want %s", source.image, sum, source.sha256)
	}
	z, err := gzip.NewReader(bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	defer z.Close()
	reader := tar.NewReader(z)
	locks := make(map[string][]byte)
	prefix := "node-" + source.commit + "/tools/"
	for {
		header, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		cohort := ""
		for _, name := range []string{"doc", "eslint"} {
			if header.Name == prefix+name+"/npm-shrinkwrap.json" {
				return nil, fmt.Errorf("%s overrides the audited package lock", header.Name)
			}
			if header.Name == prefix+name+"/package-lock.json" {
				cohort = name
			}
		}
		if cohort == "" {
			continue
		}
		if _, ok := locks[cohort]; ok || header.Typeflag != tar.TypeReg {
			return nil, fmt.Errorf("%s: duplicate or nonregular lock", header.Name)
		}
		locks[cohort], err = io.ReadAll(reader)
		if err != nil {
			return nil, err
		}
	}
	var deps []dep
	for _, cohort := range []string{"doc", "eslint"} {
		body, ok := locks[cohort]
		if !ok {
			return nil, fmt.Errorf("%s: missing tools/%s/package-lock.json", source.image, cohort)
		}
		packages, err := npmDependencies(source.image+"/"+cohort, body)
		if err != nil {
			return nil, fmt.Errorf("%s/%s: %w", source.image, cohort, err)
		}
		deps = append(deps, packages...)
	}
	return deps, nil
}

type npmPackage struct {
	Name, Version, Resolved, Integrity string
	InBundle, Link                     bool
}

var npmPathRE = regexp.MustCompile(`^node_modules/(?:@[^/]+/)?[^/]+(?:/node_modules/(?:@[^/]+/)?[^/]+)*$`)

// Audit the whole canonical lock, including optional and platform-specific
// artifacts. This is an audit closure, not a claim that npm installs every row.
// https://docs.npmjs.com/cli/v11/configuring-npm/package-lock-json#packages
func npmDependencies(cohort string, body []byte) ([]dep, error) {
	var lock struct {
		LockfileVersion int
		Packages        map[string]npmPackage
	}
	if err := json.Unmarshal(body, &lock); err != nil {
		return nil, err
	}
	if _, ok := lock.Packages[""]; lock.LockfileVersion != 3 || !ok || len(lock.Packages) < 2 {
		return nil, errors.New("expected npm lockfileVersion 3 with root and dependency packages")
	}
	var deps []dep
	for _, name := range slices.Sorted(maps.Keys(lock.Packages)) {
		if name == "" {
			continue
		}
		p := lock.Packages[name]
		if !fs.ValidPath(name) || !npmPathRE.MatchString(name) || strings.ContainsAny(name, "\\ \t\r\n?#") || p.Version == "" || p.Link {
			return nil, fmt.Errorf("invalid or unsupported npm package %q", name)
		}
		artifact, owner := p, name
		if p.InBundle {
			if p.Resolved != "" || p.Integrity != "" {
				return nil, fmt.Errorf("bundled package %s also declares an independent archive", name)
			}
			for artifact.InBundle {
				index := strings.LastIndex(owner, "/node_modules/")
				if index < 0 {
					return nil, fmt.Errorf("bundled package %s has no containing package", name)
				}
				owner = owner[:index]
				var ok bool
				if artifact, ok = lock.Packages[owner]; !ok {
					return nil, fmt.Errorf("bundled package %s has no containing lock entry %s", name, owner)
				}
			}
		}
		u, err := url.Parse(artifact.Resolved)
		if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.Fragment != "" || !validNpmIntegrity(artifact.Integrity) {
			return nil, fmt.Errorf("%s: expected an HTTPS npm archive and SHA512 integrity pin", name)
		}
		packageName := p.Name
		if packageName == "" {
			_, packageName, _ = strings.Cut(name, "node_modules/")
			if index := strings.LastIndex(packageName, "/node_modules/"); index >= 0 {
				packageName = packageName[index+len("/node_modules/"):]
			}
		}
		d := dep{name: "npm/" + cohort + "/" + name, kind: kindNpm, url: artifact.Resolved, integrity: artifact.Integrity, version: p.Version, npmName: packageName}
		if owner != name {
			d.npmPath = strings.TrimPrefix(name, owner+"/")
		}
		deps = append(deps, d)
	}
	return deps, nil
}

func validNpmIntegrity(integrity string) bool {
	encoded, ok := strings.CutPrefix(integrity, "sha512-")
	digest, err := base64.StdEncoding.Strict().DecodeString(encoded)
	return ok && err == nil && len(digest) == sha512.Size && base64.StdEncoding.EncodeToString(digest) == encoded
}

// canRetainNpm prevents failed discovery from keeping terms for a different
// locked package or archive. SHA256 is computed only after checking Integrity.
func (d dep) canRetainNpm(e Entry) bool {
	return e.Version == d.source() && e.Integrity == d.integrity &&
		artifactSHA256RE.MatchString(e.SHA256)
}

func npmArtifactHash(body []byte, integrity string) (string, error) {
	sum := sha512.Sum512(body)
	if !validNpmIntegrity(integrity) || "sha512-"+base64.StdEncoding.EncodeToString(sum[:]) != integrity {
		return "", errors.New("npm archive SHA512 integrity mismatch")
	}
	hash := sha256.Sum256(body)
	return hex.EncodeToString(hash[:]), nil
}

func fetchNpmLicense(d dep, previous Entry, get func(string, map[string]string) ([]byte, error)) (*fetched, error) {
	commit, err := licenseSourceCommit(d, previous)
	if err != nil {
		return nil, err
	}
	body, err := get(d.url, nil)
	if err != nil {
		return nil, err
	}
	hash, err := npmArtifactHash(body, d.integrity)
	if err != nil {
		return nil, err
	}
	if commit != "" {
		if hash != previous.SHA256 {
			return nil, errors.New("license_source npm archive differs from the audited SHA256")
		}
		body, err := get(previous.LicenseSource, nil)
		if err != nil {
			return nil, err
		}
		licenses, err := classify(string(body))
		if err != nil {
			return nil, err
		}
		return &fetched{commit: commit, sha256: hash, license: licenses}, nil
	}
	licenses, err := npmLicenses(d, body)
	if err != nil {
		return nil, err
	}
	return &fetched{sha256: hash, license: licenses}, nil
}

func npmLicenses(d dep, body []byte) (Licenses, error) {
	z, err := gzip.NewReader(bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	defer z.Close()
	reader := tar.NewReader(z)
	var root string
	prefix := d.npmPath
	if prefix != "" {
		prefix += "/"
	}
	files := make(map[string][]byte)
	var notices, readmes []string
	for {
		header, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		// npm removes the archive's leading directory when unpacking. Registry
		// archives use both "package" and other names, including @types names.
		name := strings.TrimSuffix(header.Name, "/")
		if !fs.ValidPath(name) || strings.Contains(name, `\`) {
			return nil, fmt.Errorf("%q: invalid npm archive path", header.Name)
		}
		directory, relative, nested := strings.Cut(name, "/")
		if root == "" {
			root = directory
		}
		if directory != root {
			return nil, fmt.Errorf("%q: npm archive has multiple roots", header.Name)
		}
		if !nested {
			if header.Typeflag != tar.TypeDir {
				return nil, fmt.Errorf("%q: npm archive entry has no leading directory", header.Name)
			}
			continue
		}
		name, ok := strings.CutPrefix(relative, prefix)
		if !ok || strings.Contains(name, "/") || name == "" {
			continue
		}
		stem := strings.ToUpper(name)
		if index := strings.IndexAny(stem, ".-_"); index >= 0 {
			stem = stem[:index]
		}
		if name != "package.json" && stem != "LICENSE" && stem != "LICENCE" && stem != "COPYING" && stem != "README" {
			continue
		}
		if _, ok := files[name]; ok || header.Typeflag != tar.TypeReg {
			return nil, fmt.Errorf("%s: duplicate or nonregular npm notice/manifest", header.Name)
		}
		files[name], err = io.ReadAll(reader)
		if err != nil {
			return nil, err
		}
		switch stem {
		case "LICENSE", "LICENCE", "COPYING":
			notices = append(notices, name)
		case "README":
			readmes = append(readmes, name)
		}
	}
	prefix = path.Join(root, d.npmPath) + "/"
	var manifest struct{ Name, Version string }
	if err := json.Unmarshal(files["package.json"], &manifest); err != nil {
		return nil, fmt.Errorf("%s: missing or invalid package manifest: %w", prefix, err)
	}
	if manifest.Name != d.npmName || manifest.Version != d.version {
		return nil, fmt.Errorf("%s: manifest %s@%s differs from locked %s@%s", prefix, manifest.Name, manifest.Version, d.npmName, d.version)
	}
	if len(notices) == 0 {
		notices = readmes
	}
	if len(notices) == 0 {
		return nil, fmt.Errorf("%s: no package license notice", prefix)
	}
	var licenses Licenses
	for _, name := range notices {
		ids, err := classify(string(files[name]))
		if err != nil {
			return nil, fmt.Errorf("%s%s: %w", prefix, name, err)
		}
		licenses = append(licenses, ids...)
	}
	slices.Sort(licenses)
	return slices.Compact(licenses), nil
}
