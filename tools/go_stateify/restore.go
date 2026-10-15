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

// restoreSet records exact type allocation and generated-loader lifetime.
// The generated field expressions are checked by the ordinary Go compiler.
type restoreSet struct {
	registered  map[string]struct{}
	modes       map[string]string
	waitValue   string
	noWaitValue string
}

func checkedRestores(file *ast.File, info *types.Info, pkg *types.Package, group *groupModel) (*restoreSet, error) {
	r := &restoreSet{registered: make(map[string]struct{}), modes: make(map[string]string), waitValue: "true", noWaitValue: "false"}
	// A package or import may shadow the predeclared boolean identifier.
	if _, value := info.Scopes[file].LookupParent("true", token.NoPos); value != types.Universe.Lookup("true") {
		r.waitValue = "0 == 0"
	}
	if _, value := info.Scopes[file].LookupParent("false", token.NoPos); value != types.Universe.Lookup("false") {
		r.noWaitValue = "0 != 0"
	}
	if len(group.types) == 0 {
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
	for _, model := range group.types {
		name := model.spec.Name.Name
		owner, ok := pkg.Scope().Lookup(name).(*types.TypeName)
		if !ok {
			return nil, fmt.Errorf("no checked restore type %s", name)
		}
		_, structural := owner.Type().Underlying().(*types.Struct)
		if structural && types.Implements(types.NewPointer(owner.Type()), loader) {
			r.registered[name] = struct{}{}
		}
		if !model.save {
			continue
		}
		if _, native := model.spec.Type.(*ast.StructType); !native {
			continue
		}
		// Only this exact native generated method has the borrowing contract.
		// Identtype forwarding, promoted methods and handwritten loaders may
		// expose Source, even when their ordinary field layouts look similar.
		r.modes[name] = "LoadGenerated"
	}
	return r, nil
}
