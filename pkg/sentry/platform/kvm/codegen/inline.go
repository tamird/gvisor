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

// Package codegen inspects production atomic pointer call sites.
package codegen

import (
	"bytes"
	"debug/dwarf"
	"debug/elf"
	"fmt"
	"go/ast"
	"go/format"
	"go/parser"
	"go/token"
	"path/filepath"
	"strings"
	"unicode"
)

// Inline is a live inlined subroutine in a concrete production function.
type Inline struct {
	Owner  string
	Caller string
	Callee string
	File   string
	Line   int64
	Ranges [][2]uint64
}

type scope struct {
	owner  string
	caller string
	files  []*dwarf.LineFile
}

func name(data *dwarf.Data, entry *dwarf.Entry) (string, error) {
	visited := make(map[dwarf.Offset]bool)
	for entry != nil {
		if value, ok := entry.Val(dwarf.AttrName).(string); ok {
			return value, nil
		}
		offset, ok := entry.Val(dwarf.AttrAbstractOrigin).(dwarf.Offset)
		if !ok || visited[offset] {
			return "", fmt.Errorf("unresolved DWARF origin at %v", entry.Offset)
		}
		visited[offset] = true
		reader := data.Reader()
		reader.Seek(offset)
		var err error
		entry, err = reader.Next()
		if err != nil {
			return "", err
		}
	}
	return "", fmt.Errorf("missing DWARF origin")
}

// Read reads live inline records from an unstripped production ELF.
func Read(path, arch string) ([]Inline, error) {
	file, err := elf.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	machine := map[string]elf.Machine{"amd64": elf.EM_X86_64, "arm64": elf.EM_AARCH64}[arch]
	if machine == elf.EM_NONE || file.Machine != machine {
		return nil, fmt.Errorf("ELF machine %v does not match %q", file.Machine, arch)
	}
	data, err := file.DWARF()
	if err != nil {
		return nil, err
	}
	return read(data)
}

func read(data *dwarf.Data) ([]Inline, error) {
	reader := data.Reader()
	var stack []scope
	var records []Inline
	for {
		entry, err := reader.Next()
		if err != nil {
			return nil, err
		}
		if entry == nil {
			break
		}
		if entry.Tag == 0 {
			if len(stack) == 0 {
				return nil, fmt.Errorf("unbalanced DWARF scope")
			}
			stack = stack[:len(stack)-1]
			continue
		}
		var current scope
		if len(stack) != 0 {
			current = stack[len(stack)-1]
		}
		switch entry.Tag {
		case dwarf.TagCompileUnit:
			lines, err := data.LineReader(entry)
			if err != nil {
				return nil, fmt.Errorf("compilation-unit line table: %w", err)
			}
			current = scope{}
			if lines != nil {
				current.files = lines.Files()
			}
		case dwarf.TagSubprogram:
			current.caller, _ = name(data, entry)
			current.owner = current.caller
		case dwarf.TagInlinedSubroutine:
			callee, err := name(data, entry)
			selected := current.owner == kvm+".seccompMmapHandler" || current.owner == kernel+".(*FDTable).get" || current.owner == kernel+".(*FDTable).swap"
			if err != nil && selected {
				return nil, err
			}
			if selected {
				if current.files == nil {
					return nil, fmt.Errorf("missing line table for selected inline owner %q", current.owner)
				}
				ranges, err := data.Ranges(entry)
				if err != nil {
					return nil, err
				}
				index, fileOK := entry.Val(dwarf.AttrCallFile).(int64)
				line, lineOK := entry.Val(dwarf.AttrCallLine).(int64)
				if current.caller != "" && fileOK && lineOK && index >= 0 && index < int64(len(current.files)) && current.files[index] != nil && len(ranges) != 0 {
					records = append(records, Inline{Owner: current.owner, Caller: current.caller, Callee: callee, File: current.files[index].Name, Line: line, Ranges: ranges})
				}
			}
			current.owner = callee
		}
		if entry.Children {
			stack = append(stack, current)
		}
	}
	if len(records) == 0 {
		return nil, fmt.Errorf("no live DWARF inline records")
	}
	return records, nil
}

// Site identifies an actual source call and its required inlined wrapper.
type Site struct {
	Caller   string
	Receiver string
	Method   string
	Callee   string
	File     string
	Line     int64
}

const kernel = "gvisor.dev/gvisor/pkg/sentry/kernel"
const kvm = "gvisor.dev/gvisor/pkg/sentry/platform/kvm"

// Sites binds the fixed production requirements to their current source positions.
func Sites(filename string, source []byte) ([]Site, error) {
	var wanted []Site
	switch filepath.Base(filename) {
	case "seccomp_mmap_unsafe.go":
		wanted = []Site{{Caller: kvm + ".seccompMmapHandler", Receiver: "machinePool[i]", Method: "Load", Callee: kvm + ".(*machineAtomicPtr).Load"}}
	case "fd_table_unsafe.go":
		wanted = []Site{
			{Caller: kernel + ".(*FDTable).get", Receiver: "f.slice", Method: "Load", Callee: kernel + ".(*descriptorBucketSliceAtomicPtr).Load"},
			{Caller: kernel + ".(*FDTable).get", Receiver: "slice[bucketN]", Method: "Load", Callee: kernel + ".(*descriptorBucketAtomicPtr).Load"},
			{Caller: kernel + ".(*FDTable).get", Receiver: "bucket[fd&fdsPerBucketMask]", Method: "Load", Callee: kernel + ".(*descriptorAtomicPtr).Load"},
			{Caller: kernel + ".(*FDTable).swap", Receiver: "f.slice", Method: "Store", Callee: kernel + ".(*descriptorBucketSliceAtomicPtr).Store"},
			{Caller: kernel + ".(*FDTable).swap", Receiver: "slice[bucketN]", Method: "Store", Callee: kernel + ".(*descriptorBucketAtomicPtr).Store"},
			{Caller: kernel + ".(*FDTable).swap", Receiver: "bucket[fd%fdsPerBucket]", Method: "Swap", Callee: kernel + ".(*descriptorAtomicPtr).Swap"},
		}
	default:
		return nil, fmt.Errorf("unexpected production source %q", filename)
	}
	positions := token.NewFileSet()
	file, err := parser.ParseFile(positions, filename, source, parser.ParseComments)
	if err != nil {
		return nil, err
	}
	for i := range wanted {
		var lines []int64
		for _, declaration := range file.Decls {
			function, ok := declaration.(*ast.FuncDecl)
			if !ok || !strings.HasSuffix(wanted[i].Caller, "."+function.Name.Name) {
				continue
			}
			ast.Inspect(function.Body, func(node ast.Node) bool {
				call, ok := node.(*ast.CallExpr)
				if !ok {
					return true
				}
				selector, ok := call.Fun.(*ast.SelectorExpr)
				if !ok || selector.Sel.Name != wanted[i].Method {
					return true
				}
				var receiver bytes.Buffer
				if err := format.Node(&receiver, positions, selector.X); err != nil {
					return true
				}
				compact := strings.Map(func(r rune) rune {
					if unicode.IsSpace(r) {
						return -1
					}
					return r
				}, receiver.String())
				if compact == wanted[i].Receiver {
					lines = append(lines, int64(positions.Position(call.Pos()).Line))
				}
				return true
			})
		}
		if len(lines) != 1 {
			return nil, fmt.Errorf("%s %s.%s: expected one source site, found %v", wanted[i].Caller, wanted[i].Receiver, wanted[i].Method, lines)
		}
		wanted[i].File = filename
		wanted[i].Line = lines[0]
	}
	return wanted, nil
}

// Validate requires live actual-site inlining, independently of call absence.
func Validate(records []Inline, sites []Site) error {
	if len(sites) == 0 {
		return fmt.Errorf("no required production sites")
	}
	for _, site := range sites {
		found := false
		for _, record := range records {
			if record.Owner == site.Caller && record.Caller != "" && record.Callee == site.Callee && record.Line == site.Line && (filepath.ToSlash(record.File) == site.File || strings.HasSuffix(filepath.ToSlash(record.File), "/"+site.File)) {
				for _, interval := range record.Ranges {
					found = found || interval[0] < interval[1]
				}
			}
		}
		if !found {
			return fmt.Errorf("missing live inline evidence: %s at %s:%d into %s", site.Callee, site.File, site.Line, site.Caller)
		}
	}
	return nil
}

// NoSplit requires an explicit directive on one production declaration.
func NoSplit(filename string, source []byte, functionName string) error {
	file, err := parser.ParseFile(token.NewFileSet(), filename, source, parser.ParseComments)
	if err != nil {
		return err
	}
	matched := 0
	for _, declaration := range file.Decls {
		function, ok := declaration.(*ast.FuncDecl)
		if !ok || function.Name.Name != functionName {
			continue
		}
		matched++
		found := false
		if function.Doc != nil {
			for _, comment := range function.Doc.List {
				found = found || comment.Text == "//go:nosplit"
			}
		}
		if !found {
			return fmt.Errorf("%s in %s lacks go:nosplit", functionName, filename)
		}
	}
	if matched != 1 {
		return fmt.Errorf("expected one %s declaration in %s, got %d", functionName, filename, matched)
	}
	return nil
}

// MmapHandlerApplicable checks the source and ELF evidence for the mmap handler.
// ARM64 maps the entire address space, so its production link omits this path.
func MmapHandlerApplicable(path string, machineSource, archSource []byte) (bool, error) {
	file, err := elf.Open(path)
	if err != nil {
		return false, err
	}
	defer file.Close()
	symbols, err := file.Symbols()
	if err != nil {
		return false, err
	}
	return mmapHandlerApplicable(file.Machine, symbols, machineSource, archSource)
}

func mmapHandlerApplicable(machine elf.Machine, symbols []elf.Symbol, machineSource, archSource []byte) (bool, error) {
	if machine != elf.EM_X86_64 && machine != elf.EM_AARCH64 {
		return false, fmt.Errorf("unexpected production ELF machine %v", machine)
	}
	normalize := func(source []byte) (string, error) {
		positions := token.NewFileSet()
		file, err := parser.ParseFile(positions, "source.go", source, 0)
		if err != nil {
			return "", err
		}
		var result bytes.Buffer
		if err := format.Node(&result, positions, file); err != nil {
			return "", err
		}
		return result.String(), nil
	}
	arch, err := normalize(archSource)
	if err != nil {
		return false, err
	}
	want := "false"
	if machine == elf.EM_AARCH64 {
		want = "true"
	}
	if strings.Count(arch, "mapEntireAddressSpaceDefault") != 1 || !strings.Contains(arch, "const mapEntireAddressSpaceDefault = "+want+"\n") {
		return false, fmt.Errorf("address-space default does not match %v", machine)
	}
	source, err := normalize(machineSource)
	if err != nil {
		return false, err
	}
	const installation = `mapEntireAddressSpace := forceMappingEntireAddressSpace || mapEntireAddressSpaceDefault
	if mapEntireAddressSpace {
		archOverrideFaultBlocks()
	} else {
		seccompMmapRules(m)
	}`
	// Bind the only installation call and its immediately preceding condition.
	// A changed source path requires the applicability assumption to be reviewed.
	if strings.Count(source, "mapEntireAddressSpaceDefault") != 1 || strings.Count(source, "seccompMmapRules(") != 2 || !strings.Contains(source, installation) {
		return false, fmt.Errorf("unexpected mmap handler installation source")
	}
	for _, name := range []string{"seccompMmapHandler", "seccompMmapRules", "sigsysHandler", "addrOfSigsysHandler"} {
		found := false
		for _, symbol := range symbols {
			if symbol.Section != elf.SHN_UNDEF && elf.ST_TYPE(symbol.Info) == elf.STT_FUNC && (symbol.Name == kvm+"."+name || strings.HasPrefix(symbol.Name, kvm+"."+name+".")) {
				found = true
			}
		}
		if found != (machine == elf.EM_X86_64) {
			return false, fmt.Errorf("unexpected production TEXT presence for %s on %v: %t", name, machine, found)
		}
	}
	return machine == elf.EM_X86_64, nil
}
