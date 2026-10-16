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

package main

import (
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
)

// restoreSet selects shared typed scalar operations from compiler-owned field
// types. Containers and references retain Source.Load's common graph engine.
type restoreSet struct {
	fields      map[string]map[string]string
	waitValue   string
	noWaitValue string
}

func checkedRestores(file *ast.File, info *types.Info, pkg *types.Package, group *groupModel) (*restoreSet, error) {
	r := &restoreSet{fields: make(map[string]map[string]string), waitValue: "true", noWaitValue: "false"}
	// A package or import may shadow the predeclared boolean identifier.
	if _, value := info.Scopes[file].LookupParent("true", token.NoPos); value != types.Universe.Lookup("true") {
		r.waitValue = "0 == 0"
	}
	if _, value := info.Scopes[file].LookupParent("false", token.NoPos); value != types.Universe.Lookup("false") {
		r.noWaitValue = "0 != 0"
	}
	for _, model := range group.types {
		if !model.save {
			continue
		}
		if _, native := model.spec.Type.(*ast.StructType); !native {
			continue
		}
		name := model.spec.Name.Name
		owner, ok := pkg.Scope().Lookup(name).(*types.TypeName)
		if !ok {
			return nil, fmt.Errorf("no checked restore type %s", name)
		}
		fields := make(map[string]string)
		for _, field := range model.fields {
			if field.kind != fieldNormal && field.kind != fieldWait {
				continue
			}
			member, _, _ := types.LookupFieldOrMethod(types.NewPointer(owner.Type()), true, pkg, field.name)
			if member == nil {
				return nil, fmt.Errorf("%s has no checked field %s", name, field.name)
			}
			basic, ok := member.Type().Underlying().(*types.Basic)
			if !ok {
				continue
			}
			switch basic.Kind() {
			case types.Bool:
				fields[field.name] = "LoadBool"
			case types.Int, types.Int8, types.Int16, types.Int32, types.Int64:
				fields[field.name] = "LoadInt"
			case types.Uint, types.Uint8, types.Uint16, types.Uint32, types.Uint64, types.Uintptr:
				fields[field.name] = "LoadUint"
			case types.Float32, types.Float64:
				fields[field.name] = "LoadFloat"
			case types.Complex64, types.Complex128:
				fields[field.name] = "LoadComplex"
			case types.String:
				fields[field.name] = "LoadString"
			}
		}
		r.fields[name] = fields
	}
	return r, nil
}
