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
	"strconv"
)

// restoreSet contains data for native generated fields, not specialized
// traversal functions. Actual child types and offsets are resolved by state.
type restoreSet struct {
	registered map[string]struct{}
	fields     map[string][]restoreField
	waitValue  string
}

type restoreField struct {
	index int
	slot  int
	wait  bool
}

func checkedRestores(file *ast.File, info *types.Info, pkg *types.Package, model *groupModel) (*restoreSet, error) {
	r := &restoreSet{registered: make(map[string]struct{}), fields: make(map[string][]restoreField), waitValue: "true"}
	// A package or import may shadow the predeclared boolean identifier.
	if _, value := info.Scopes[file].LookupParent("true", token.NoPos); value != types.Universe.Lookup("true") {
		r.waitValue = "0 == 0"
	}
	if len(model.types) == 0 {
		return r, nil
	}
	statePackage := pkg
	for _, spec := range file.Imports {
		path, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			return nil, err
		}
		if path == statePkg {
			statePackage = info.Implicits[spec].(*types.PkgName).Imported()
		}
	}
	object := statePackage.Scope().Lookup("SaverLoader")
	if object == nil {
		return nil, fmt.Errorf("no checked state.SaverLoader interface")
	}
	loader := object.Type().Underlying().(*types.Interface)
	for _, model := range model.types {
		name := model.spec.Name.Name
		owner, ok := pkg.Scope().Lookup(name).(*types.TypeName)
		if !ok {
			return nil, fmt.Errorf("no checked restore type %s", name)
		}
		structure, structural := owner.Type().Underlying().(*types.Struct)
		if structural && types.Implements(types.NewPointer(owner.Type()), loader) {
			r.registered[name] = struct{}{}
		}
		if !model.save {
			continue
		}
		if _, native := model.spec.Type.(*ast.StructType); !native {
			continue
		}
		for _, field := range model.fields {
			if field.kind != fieldNormal && field.kind != fieldWait {
				continue
			}
			member, index, _ := types.LookupFieldOrMethod(types.NewPointer(owner.Type()), true, pkg, field.name)
			if member == nil || len(index) != 1 || structure.Field(index[0]).Name() != field.name {
				return nil, fmt.Errorf("%s has no direct checked restore field %s", name, field.name)
			}
			r.fields[name] = append(r.fields[name], restoreField{index: index[0], slot: field.slot, wait: field.kind == fieldWait})
		}
	}
	return r, nil
}
