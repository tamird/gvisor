// Copyright 2018 The gVisor Authors.
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

// Command check verifies that the Sentry can map a vDSO directly from its ELF.
package main

import (
	"debug/elf"
	"flag"
	"fmt"
	"log"
	"strings"
)

func main() {
	path := flag.String("vdso", "", "path to the vDSO ELF")
	checkData := flag.Bool("check-data", false, "require nonempty .text and reject .data and .bss sections with contents")
	flag.Parse()
	log.SetFlags(0)
	if *path == "" || flag.NArg() != 0 {
		log.Fatal("usage: check --vdso=FILE [--check-data]")
	}
	f, err := elf.Open(*path)
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close()
	if err := check(f, *checkData); err != nil {
		log.Fatalf("%s: %v", *path, err)
	}
}

func check(f *elf.File, checkData bool) error {
	// The Sentry maps the ELF file directly. Each load segment must already
	// occupy its expected offset in memory, with no zero-fill required.
	const pageSize = 4096 // The vDSO linker uses 4 KiB pages on both architectures.
	var first, prev *elf.Prog
	for _, p := range f.Progs {
		if p.Type != elf.PT_LOAD {
			continue
		}
		if first == nil {
			first = p
		}
		if p.Vaddr < first.Vaddr || p.Vaddr-first.Vaddr != p.Off {
			return fmt.Errorf("PT_LOAD at %#x has file offset %#x, want its offset from %#x", p.Vaddr, p.Off, first.Vaddr)
		}
		if p.Filesz != p.Memsz {
			return fmt.Errorf("PT_LOAD at %#x has file size %#x and memory size %#x", p.Vaddr, p.Filesz, p.Memsz)
		}
		if prev != nil {
			if p.Vaddr < prev.Vaddr || p.Vaddr-prev.Vaddr < prev.Memsz {
				return fmt.Errorf("PT_LOAD at %#x overlaps or precedes PT_LOAD at %#x", p.Vaddr, prev.Vaddr)
			}
			// Match validateVDSO in pkg/sentry/loader. It protects whole
			// pages and compares the previous segment's exclusive end.
			// An end exactly on the next segment's page boundary fails too.
			if prev.Vaddr+prev.Memsz >= p.Vaddr&^(pageSize-1) {
				return fmt.Errorf("PT_LOAD end %#x and next start %#x are on the same page", prev.Vaddr+prev.Memsz, p.Vaddr)
			}
		}
		prev = p
	}
	if first == nil {
		return fmt.Errorf("no PT_LOAD segments")
	}

	// The generic ABI defines these values; Go's debug/elf does not yet name them.
	// https://gabi.xinuos.com/elf/03-sheader.html#section-type
	// https://gabi.xinuos.com/elf/08-dynamic.html#dynamic-array-tags
	const (
		shtRelr  elf.SectionType = 19
		dtRelrSz elf.DynTag      = 35
	)
	text := false
	for _, s := range f.Sections {
		// GCC may emit empty .data and .bss sections where Clang omits them.
		if s.Size == 0 {
			continue
		}
		if checkData && (strings.HasPrefix(s.Name, ".data") || strings.HasPrefix(s.Name, ".bss")) {
			return fmt.Errorf("unexpected nonempty section %s", s.Name)
		}
		if s.Name == ".text" {
			text = true
		}
		switch s.Type {
		case elf.SHT_REL, elf.SHT_RELA, shtRelr:
			return fmt.Errorf("unexpected relocation section %s", s.Name)
		}
	}
	if checkData && !text {
		return fmt.Errorf("no nonempty .text section")
	}
	for _, tag := range []elf.DynTag{elf.DT_RELSZ, elf.DT_RELASZ, dtRelrSz, elf.DT_PLTRELSZ} {
		sizes, err := f.DynValue(tag)
		if err != nil {
			return err
		}
		for _, size := range sizes {
			if size != 0 {
				return fmt.Errorf("dynamic relocation tag %v has size %d", tag, size)
			}
		}
	}
	return nil
}
