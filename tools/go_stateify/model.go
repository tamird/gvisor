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

// Stateify provides a simple way to generate Load/Save methods based on
// existing types and struct tags.
package main

import (
	"go/ast"
	"go/token"
	"strings"
)

type method struct {
	typeName   string
	methodName string
}

// fieldModel is the single owner of field numbering and save/load ordering.
// A zero-value assertion has no slot. Custom value fields are captured before
// ordinary fields, but retain their position in the saved schema.
type fieldKind int

const (
	fieldZero fieldKind = iota
	fieldNormal
	fieldWait
	fieldValue
)

type fieldModel struct {
	name      string
	valueType string
	kind      fieldKind
	slot      int
}

type typeModel struct {
	spec      *ast.TypeSpec
	receiver  string
	save      bool
	identical bool
	fields    []fieldModel
}

func (m *typeModel) scan(fn scanFunctions) {
	for _, f := range m.fields {
		switch f.kind {
		case fieldZero:
			if fn.zerovalue != nil {
				fn.zerovalue(f.name)
			}
		case fieldNormal:
			if fn.normal != nil {
				fn.normal(f.name)
			}
		case fieldWait:
			if fn.wait != nil {
				fn.wait(f.name)
			}
		case fieldValue:
			if fn.value != nil {
				fn.value(f.name, f.valueType)
			}
		}
	}
}

type groupModel struct {
	types   []*typeModel
	methods map[method]struct{}
}

func modelFor(files []*ast.File) *groupModel {
	group := &groupModel{methods: make(map[method]struct{})}
	receivers := make(map[string]string)
	for _, f := range files {
		for _, d := range f.Decls {
			fn, ok := d.(*ast.FuncDecl)
			if !ok || fn.Recv == nil || len(fn.Recv.List) != 1 {
				continue
			}
			name, _ := resolveTypeName(fn.Recv.List[0].Type)
			group.methods[method{name, fn.Name.Name}] = struct{}{}
			if len(fn.Recv.List[0].Names) != 0 {
				receivers[name] = fn.Recv.List[0].Names[0].Name
			}
		}
	}
	for _, f := range files {
		for _, declaration := range f.Decls {
			d, ok := declaration.(*ast.GenDecl)
			if !ok || d.Tok != token.TYPE || d.Doc == nil {
				continue
			}
			marked, save, identical := false, false, false
			for _, c := range d.Doc.List {
				switch c.Text {
				case "// +stateify savable":
					marked, save = true, true
				case "// +stateify type":
					marked = true
				case "// +stateify identtype":
					identical = true
				}
			}
			if !marked {
				continue
			}
			for _, spec := range d.Specs {
				ts := spec.(*ast.TypeSpec)
				// An alias does not declare a new Go type or method owner.
				if ts.Assign.IsValid() {
					continue
				}
				recv, ok := receivers[ts.Name.Name]
				if !ok {
					recv = strings.ToLower(ts.Name.Name[:1])
				}
				model := &typeModel{spec: ts, receiver: recv, save: save, identical: identical}
				if st, ok := ts.Type.(*ast.StructType); ok {
					slot := 0
					appendField := func(name string, kind fieldKind, valueType string) {
						index := slot
						if kind == fieldZero {
							index = -1
						} else {
							slot++
						}
						model.fields = append(model.fields, fieldModel{name: name, kind: kind, valueType: valueType, slot: index})
					}
					scanFields(st, scanFunctions{
						zerovalue: func(name string) { appendField(name, fieldZero, "") },
						normal:    func(name string) { appendField(name, fieldNormal, "") },
						wait:      func(name string) { appendField(name, fieldWait, "") },
						value:     func(name, typ string) { appendField(name, fieldValue, typ) },
					})
				}
				group.types = append(group.types, model)
			}
		}
	}
	return group
}
