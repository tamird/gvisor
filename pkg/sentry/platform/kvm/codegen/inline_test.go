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

import (
	"bytes"
	"debug/dwarf"
	"debug/elf"
	"encoding/binary"
	"os"
	"strings"
	"testing"

	"gvisor.dev/gvisor/pkg/test/testutil"
)

func TestValidateActualSite(t *testing.T) {
	site := Site{Caller: kvm + ".seccompMmapHandler", Callee: kvm + ".(*machineAtomicPtr).Load", File: "pkg/sentry/platform/kvm/seccomp_mmap_unsafe.go", Line: 42}
	valid := Inline{Owner: site.Caller, Caller: site.Caller, Callee: site.Callee, File: "gvisor.dev/gvisor/" + site.File, Line: site.Line, Ranges: [][2]uint64{{100, 104}}}
	for _, test := range []struct {
		name   string
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
		name     string
		comment  string
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

// TestCompilationUnitWithoutLines distinguishes valid non-code units from
// selected inline evidence that requires source positions.
func TestCompilationUnitWithoutLines(t *testing.T) {
	for _, selected := range []bool{false, true} {
		// DWARF4 abbreviations: compilation unit, subprogram and inline entry,
		// each with an inline string name. Only the first two have children.
		abbrev := []byte{1, 0x11, 1, 3, 8, 0, 0, 2, 0x2e, 1, 3, 8, 0, 0, 3, 0x1d, 0, 3, 8, 0, 0, 0}
		body := append([]byte{1}, append([]byte("unrelated"), 0)...)
		if selected {
			body = append(body, 2)
			body = append(body, []byte(kvm+".seccompMmapHandler")...)
			body = append(body, 0, 3)
			body = append(body, []byte(kvm+".(*machineAtomicPtr).Load")...)
			body = append(body, 0, 0)
		}
		body = append(body, 0)
		var info bytes.Buffer
		for _, value := range []any{uint32(len(body) + 7), uint16(4), uint32(0), uint8(8)} {
			if err := binary.Write(&info, binary.LittleEndian, value); err != nil {
				t.Fatal(err)
			}
		}
		info.Write(body)
		data, err := dwarf.New(abbrev, nil, nil, info.Bytes(), nil, nil, nil, nil)
		if err != nil {
			t.Fatal(err)
		}
		_, err = read(data)
		want := "no live DWARF inline records"
		if selected {
			want = "missing line table for selected inline owner"
		}
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Fatalf("read(selected=%t) = %v, want %q", selected, err, want)
		}
	}
}

func TestMmapHandlerApplicability(t *testing.T) {
	const machineSource = `package kvm
func newMachine() {
	mapEntireAddressSpace := forceMappingEntireAddressSpace || mapEntireAddressSpaceDefault
	if mapEntireAddressSpace {
		archOverrideFaultBlocks()
	} else {
		seccompMmapRules(m)
	}
}
func seccompMmapRules(m *machine) {}
`
	amdSource := []byte("package kvm\nconst mapEntireAddressSpaceDefault = false\n")
	armSource := []byte("package kvm\nconst mapEntireAddressSpaceDefault = true\n")
	var live []elf.Symbol
	for _, name := range []string{"seccompMmapHandler", "seccompMmapRules", "sigsysHandler.abi0", "addrOfSigsysHandler.abi0"} {
		live = append(live, elf.Symbol{Name: kvm + "." + name, Info: byte(elf.STT_FUNC), Section: 1})
	}
	for _, tc := range []struct {
		name    string
		machine elf.Machine
		symbols []elf.Symbol
		arch    []byte
		source  string
		want    bool
		wantErr bool
	}{
		{name: "amd64_live", machine: elf.EM_X86_64, symbols: live, arch: amdSource, source: machineSource, want: true},
		{name: "arm64_not_live", machine: elf.EM_AARCH64, arch: armSource, source: machineSource},
		{name: "arm64_changed_default", machine: elf.EM_AARCH64, arch: amdSource, source: machineSource, wantErr: true},
		{name: "arm64_live_symbol", machine: elf.EM_AARCH64, symbols: live[:1], arch: armSource, source: machineSource, wantErr: true},
		{name: "amd64_missing_handler", machine: elf.EM_X86_64, symbols: live[1:], arch: amdSource, source: machineSource, wantErr: true},
		{name: "changed_installation", machine: elf.EM_AARCH64, arch: armSource, source: strings.Replace(machineSource, "if mapEntireAddressSpace", "if otherCondition", 1), wantErr: true},
		{name: "extra_installation", machine: elf.EM_AARCH64, arch: armSource, source: machineSource + "func another() { seccompMmapRules(m) }\n", wantErr: true},
		{name: "wrong_machine", machine: elf.EM_NONE, arch: armSource, source: machineSource, wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := mmapHandlerApplicable(tc.machine, tc.symbols, []byte(tc.source), tc.arch)
			if (err != nil) != tc.wantErr || got != tc.want {
				t.Fatalf("mmapHandlerApplicable = %t, %v; want %t, error=%t", got, err, tc.want, tc.wantErr)
			}
		})
	}
}

func TestProductionMmapHandlerApplicability(t *testing.T) {
	readSource := func(t *testing.T, path string) []byte {
		t.Helper()
		file, err := testutil.FindFile(path)
		if err != nil {
			t.Fatal(err)
		}
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		return data
	}
	machineSource := readSource(t, "pkg/sentry/platform/kvm/machine.go")
	for _, arch := range []string{"amd64", "arm64"} {
		t.Run(arch, func(t *testing.T) {
			machine := elf.EM_AARCH64
			var symbols []elf.Symbol
			if arch == "amd64" {
				machine = elf.EM_X86_64
				for _, name := range []string{"seccompMmapHandler", "seccompMmapRules", "sigsysHandler.abi0", "addrOfSigsysHandler.abi0"} {
					symbols = append(symbols, elf.Symbol{Name: kvm + "." + name, Info: byte(elf.STT_FUNC), Section: 1})
				}
			}
			archSource := readSource(t, "pkg/sentry/platform/kvm/machine_" + arch + ".go")
			got, err := mmapHandlerApplicable(machine, symbols, machineSource, archSource)
			if err != nil || got != (arch == "amd64") {
				t.Fatalf("production source applicability on %s: got %t, %v", arch, got, err)
			}
		})
	}
}
