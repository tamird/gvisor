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

package codegen

import "testing"

func TestValidateActualSite(t *testing.T) {
	site := Site{Caller: kvm + ".seccompMmapHandler", Callee: kvm + ".(*machineAtomicPtr).Load", File: "pkg/sentry/platform/kvm/seccomp_mmap_unsafe.go", Line: 42}
	valid := Inline{Owner: site.Caller, Caller: site.Caller, Callee: site.Callee, File: "gvisor.dev/gvisor/" + site.File, Line: site.Line, Ranges: [][2]uint64{{100, 104}}}
	for _, test := range []struct {
		name string
		change func(*Inline)
	}{
		{"valid", func(*Inline) {}},
		{"wrong_owner", func(r *Inline) { r.Owner += "Other" }},
		{"missing_concrete_caller", func(r *Inline) { r.Caller = "" }},
		{"wrong_origin", func(r *Inline) { r.Callee = "other.Load" }},
		{"wrong_line", func(r *Inline) { r.Line++ }},
		{"wrong_file", func(r *Inline) { r.File = "other/seccomp_mmap_unsafe.go" }},
		{"missing_range", func(r *Inline) { r.Ranges = nil }},
		{"empty_range", func(r *Inline) { r.Ranges = [][2]uint64{{100, 100}} }},
	} {
		t.Run(test.name, func(t *testing.T) {
			record := valid
			test.change(&record)
			err := Validate([]Inline{record}, []Site{site})
			if got, want := err == nil, test.name == "valid"; got != want {
				t.Fatalf("Validate() = %v, want accepted=%t", err, want)
			}
		})
	}
	if err := Validate(nil, []Site{site}); err == nil {
		t.Fatal("missing inline evidence accepted")
	}
	if err := Validate([]Inline{valid}, nil); err == nil {
		t.Fatal("empty requirements accepted")
	}
}

func TestBindProductionSourceSite(t *testing.T) {
	const filename = "pkg/sentry/platform/kvm/seccomp_mmap_unsafe.go"
	const source = "package kvm\nfunc seccompMmapHandler() { machinePool[i].Load() }\n"
	sites, err := Sites(filename, []byte(source))
	if err != nil || len(sites) != 1 || sites[0].Line != 2 {
		t.Fatalf("Sites() = %v, %v; want one site on line2", sites, err)
	}
	for _, source := range []string{
		"package kvm\nfunc seccompMmapHandler() {}",
		"package kvm\nfunc seccompMmapHandler() { machinePool[i].Load(); machinePool[i].Load() }",
	} {
		if _, err := Sites(filename, []byte(source)); err == nil {
			t.Fatalf("ambiguous/missing source site accepted: %s", source)
		}
	}
}

func TestNoSplit(t *testing.T) {
	for _, test := range []struct {
		name string
		comment string
		accepted bool
	}{
		{"directive", "//go:nosplit\n", true},
		{"missing", "", false},
		{"ordinary_comment", "// go:nosplit\n", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			err := NoSplit("production.go", []byte("package kvm\n"+test.comment+"func Load() {}"), "Load")
			if (err == nil) != test.accepted {
				t.Fatalf("NoSplit() = %v, want accepted=%t", err, test.accepted)
			}
		})
	}
}
