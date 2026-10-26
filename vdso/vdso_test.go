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

package vdso_test

import (
	"debug/elf"
	"flag"
	"strings"
	"testing"
)

var vdsoPath = flag.String("vdso", "", "Path to the VDSO ELF")

func TestVDSO(t *testing.T) {
	f, err := elf.Open(*vdsoPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := f.Close(); err != nil {
			t.Errorf("closing VDSO: %v", err)
		}
	})

	t.Run("Segments", func(t *testing.T) {
		const pageSize = 4096
		var first, previous *elf.Prog
		for _, p := range f.Progs {
			if p.Type != elf.PT_LOAD {
				continue
			}
			if first == nil {
				first = p
				if p.Off != 0 {
					t.Errorf("first PT_LOAD offset = %#x, want 0", p.Off)
				}
			}
			if p.Vaddr < first.Vaddr || p.Vaddr-first.Vaddr != p.Off {
				t.Errorf("PT_LOAD memory offset from %#x differs from file offset: %+v", first.Vaddr, p.ProgHeader)
			}
			if p.Memsz != p.Filesz {
				t.Errorf("PT_LOAD memory and file sizes differ: %+v", p.ProgHeader)
			}
			if previous != nil {
				// Subtract only after checking order, so overflowing segment ends
				// cannot make overlapping segments appear disjoint.
				if p.Vaddr < previous.Vaddr || p.Vaddr-previous.Vaddr < previous.Memsz {
					t.Fatalf("PT_LOAD segments overlap or are out of order: %+v, %+v", previous.ProgHeader, p.ProgHeader)
				}
				lastEnd := previous.Vaddr + previous.Memsz
				if lastEnd&^(pageSize-1) >= p.Vaddr&^(pageSize-1) {
					t.Errorf("PT_LOAD segments share an end page: %+v, %+v", previous.ProgHeader, p.ProgHeader)
				}
			}
			previous = p
		}
		if first == nil {
			t.Error("VDSO has no PT_LOAD segments")
		}
	})

	t.Run("Sections", func(t *testing.T) {
		foundText := false
		for _, s := range f.Sections {
			if s.Name == ".text" && s.Size != 0 {
				foundText = true
			}
			if (strings.HasPrefix(s.Name, ".data") || strings.HasPrefix(s.Name, ".bss")) && s.Size != 0 {
				t.Errorf("VDSO has nonempty data section: %+v", s.SectionHeader)
			}
		}
		if !foundText {
			t.Error("VDSO has no nonempty .text section")
		}
	})

	t.Run("Relocations", func(t *testing.T) {
		// These ELF ABI constants are not yet named by debug/elf.
		// https://gabi.xinuos.com/elf/03-sheader.html#section-type
		// https://gabi.xinuos.com/elf/08-dynamic.html
		const (
			shtRelr  elf.SectionType = 19
			dtRelrSz elf.DynTag      = 35
		)
		for _, s := range f.Sections {
			switch s.Type {
			case elf.SHT_REL, elf.SHT_RELA, shtRelr:
				if s.Size != 0 {
					t.Errorf("VDSO has a nonempty relocation section: %+v", s.SectionHeader)
				}
			}
		}
		for _, tag := range []elf.DynTag{elf.DT_RELSZ, elf.DT_RELASZ, dtRelrSz, elf.DT_PLTRELSZ} {
			values, err := f.DynValue(tag)
			if err != nil {
				t.Fatalf("reading dynamic relocation sizes: %v", err)
			}
			for _, size := range values {
				if size != 0 {
					t.Errorf("VDSO dynamic relocation size %v = %d, want 0", tag, size)
				}
			}
		}
	})
}
