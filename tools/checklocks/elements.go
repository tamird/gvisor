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

package checklocks

import (
	"go/token"
	"go/types"
	"slices"

	"golang.org/x/tools/go/ssa"
)

// elementResult describes a single-result local function whose every non-nil
// return has been proved to address an element of the same receiver's slice.
type elementResult struct {
	parameter int
	field     types.Object
}

type elementOrigin struct {
	owner ssa.Value
	field types.Object
}

func (pc *passContext) directElement(value ssa.Value) elementOrigin {
	index, ok := value.(*ssa.IndexAddr)
	if !ok {
		return elementOrigin{}
	}
	load, ok := index.X.(*ssa.UnOp)
	if !ok || load.Op != token.MUL {
		return elementOrigin{}
	}
	field, ok := load.X.(*ssa.FieldAddr)
	if !ok {
		return elementOrigin{}
	}
	obj, ok := findField(field.X.Type(), field.Field)
	if !ok || pc.elementFields[obj] == nil {
		return elementOrigin{}
	}
	return elementOrigin{field.X, obj}
}

// forwardedElement translates the inferred parameter index to the actual
// argument at this particular call.
func (pc *passContext) forwardedElement(value ssa.Value) elementOrigin {
	call, ok := value.(*ssa.Call)
	if !ok {
		return elementOrigin{}
	}
	result, ok := pc.elementResults[call.Common().StaticCallee()]
	if !ok {
		return elementOrigin{}
	}
	return elementOrigin{call.Common().Args[result.parameter], result.field}
}

func (pc *passContext) inferElementResult(fn *ssa.Function) (elementResult, bool) {
	if fn.Signature.Results().Len() != 1 {
		return elementResult{}, false
	}
	if _, ptr := fn.Signature.Results().At(0).Type().Underlying().(*types.Pointer); !ptr {
		return elementResult{}, false
	}
	var result elementResult
	for _, block := range fn.Blocks {
		ret, ok := block.Instrs[len(block.Instrs)-1].(*ssa.Return)
		if !ok {
			continue
		}
		v := ret.Results[0]
		if c, ok := v.(*ssa.Const); ok && c.IsNil() {
			continue
		}
		origin := pc.directElement(v)
		if origin.owner == nil {
			origin = pc.forwardedElement(v)
		}
		param, ok := origin.owner.(*ssa.Parameter)
		if !ok {
			return elementResult{}, false
		}
		index := slices.Index(fn.Params, param)
		current := elementResult{index, origin.field}
		if index < 0 || (result.field != nil && current != result) {
			return elementResult{}, false
		}
		result = current
	}
	return result, result.field != nil
}

func (pc *passContext) inferElementResults(functions []*ssa.Function) {
	if len(pc.elementFields) == 0 {
		return
	}
	for obj, facts := range pc.elementFields {
		valid := len(facts.GuardedBy) == 1 && !facts.ReadAny
		for _, guard := range facts.GuardedBy {
			_, local := guard.(*fieldGuard)
			valid = valid && local
		}
		if !valid {
			pc.maybeFail(obj.Pos(), "element proof requires a single owner-relative guard")
			delete(pc.elementFields, obj)
		}
	}
	pc.elementResults = make(map[*ssa.Function]elementResult)
	for {
		old := len(pc.elementResults)
		for _, fn := range functions {
			if _, known := pc.elementResults[fn]; !known {
				if result, ok := pc.inferElementResult(fn); ok {
					pc.elementResults[fn] = result
				}
			}
		}
		if len(pc.elementResults) == old {
			break
		}
	}
}

// memberAddress follows only the returned element and its physical fields.
// Pointer values loaded from its storage may have a different ownership model.
func (pc *passContext) memberAddress(v ssa.Value) elementOrigin {
	for {
		switch addr := v.(type) {
		case *ssa.FieldAddr:
			v = addr.X
		case *ssa.IndexAddr:
			if _, ptr := addr.X.Type().Underlying().(*types.Pointer); !ptr {
				return elementOrigin{}
			}
			v = addr.X
		default:
			return pc.forwardedElement(v)
		}
	}
}

// localCallsOnly admits synchronous source-visible calls with no lock API,
// asynchronous work, unsafe conversion or dynamic dispatch. It is deliberately
// narrower than a general lock-effect summary.
func localCallsOnly(fn *ssa.Function, visiting map[*ssa.Function]bool) bool {
	if fn == nil || len(fn.Blocks) == 0 || visiting[fn] {
		return false
	}
	if obj, ok := fn.Object().(*types.Func); ok && (lockerRE.MatchString(obj.FullName()) || mutexRE.MatchString(obj.FullName())) {
		return false
	}
	visiting[fn] = true
	defer delete(visiting, fn)
	for _, b := range fn.Blocks {
		for _, inst := range b.Instrs {
			switch x := inst.(type) {
			case *ssa.Go, *ssa.Defer, *ssa.MakeClosure, *ssa.Convert:
				return false
			case *ssa.Call:
				if builtin, ok := x.Common().Value.(*ssa.Builtin); ok && (builtin.Name() == "len" || builtin.Name() == "cap") {
					continue
				}
				if !localCallsOnly(x.Common().StaticCallee(), visiting) {
					return false
				}
			}
		}
	}
	return true
}

// containsType includes inline subobjects; following arbitrary pointers here
// would conflate a list head with the separately allocated node it references.
func containsType(outer, inner types.Type) bool {
	if types.Identical(outer, inner) {
		return true
	}
	switch typ := outer.Underlying().(type) {
	case *types.Struct:
		for i := 0; i < typ.NumFields(); i++ {
			if containsType(typ.Field(i).Type(), inner) {
				return true
			}
		}
	case *types.Array:
		return containsType(typ.Elem(), inner)
	}
	return false
}

// addressStaysLocal follows the actual formal through interior addresses and
// static calls. Returning or storing the address, boxing, or an opaque use
// prevents a positive result. A pointer load that could alias an interior
// address is also outside this proof.
func addressStaysLocal(v ssa.Value, outer types.Type, seen map[ssa.Value]bool) bool {
	if seen[v] {
		return true
	}
	seen[v] = true
	refs := v.Referrers()
	if refs == nil {
		return false
	}
	for _, use := range *refs {
		switch x := use.(type) {
		case *ssa.DebugRef:
		case *ssa.FieldAddr:
			if !addressStaysLocal(x, outer, seen) {
				return false
			}
		case *ssa.UnOp:
			if x.Op != token.MUL {
				return false
			}
			switch t := x.Type().Underlying().(type) {
			case *types.Pointer:
				if containsType(outer, t.Elem()) {
					return false
				}
			case *types.Basic:
				if t.Kind() == types.UnsafePointer {
					return false
				}
			case *types.Chan:
			default:
				return false
			}
		case *ssa.Store:
			if x.Addr != v || x.Val == v {
				return false
			}
		case *ssa.BinOp:
			if x.Op != token.EQL && x.Op != token.NEQ {
				return false
			}
		case *ssa.Call:
			fn := x.Common().StaticCallee()
			if fn == nil || len(fn.Blocks) == 0 {
				return false
			}
			for i, arg := range x.Common().Args {
				if arg == v && !addressStaysLocal(fn.Params[i], outer, seen) {
					return false
				}
			}
		default:
			return false
		}
	}
	return true
}

func (pc *passContext) checkElementCall(call *ssa.Call, v ssa.Value, lock string, ls *lockState) {
	common := call.Common()
	callee := common.StaticCallee()
	if callee == nil || callee.Object() == nil || len(callee.Blocks) == 0 {
		pc.maybeFail(call.Pos(), "unproven guarded element transfer to opaque callee")
		return
	}
	var facts lockFunctionFacts
	pc.importLockFunctionFacts(callee.Object().(*types.Func), &facts)
	if len(facts.HeldOnEntry) > 0 {
		match := false
		for _, guard := range facts.HeldOnEntry {
			resolved := guard.Resolver.resolveCall(pc, ls, common.Args, call)
			if !resolved.valid() {
				continue
			}
			key, _ := ls.isHeld(resolved, guard.Exclusive)
			if key == lock && guard.Exclusive {
				match = true
				break
			}
		}
		if !match {
			pc.maybeFail(call.Pos(), "callee guard does not match guarded element owner %s", lock)
			return
		}
	}
	if !localCallsOnly(callee, make(map[*ssa.Function]bool)) {
		pc.maybeFail(call.Pos(), "unproven lock effects of guarded element callee")
		return
	}
	ptr := v.Type().Underlying().(*types.Pointer)
	for i, arg := range common.Args {
		if arg == v && !addressStaysLocal(callee.Params[i], ptr.Elem(), make(map[ssa.Value]bool)) {
			pc.maybeFail(call.Pos(), "guarded element address may escape in callee")
			return
		}
	}
}

func (pc *passContext) checkElementUses(inst ssa.Instruction, ops []*ssa.Value, ls *lockState) {
	for _, operand := range ops {
		if operand == nil || *operand == nil {
			continue
		}
		v := *operand
		origin := pc.memberAddress(v)
		if _, ret := inst.(*ssa.Return); ret && origin.owner == nil {
			origin = pc.directElement(v)
		}
		if origin.owner == nil {
			continue
		}
		switch x := inst.(type) {
		case *ssa.DebugRef, *ssa.FieldAddr:
			continue
		case *ssa.Return:
			if _, ok := pc.elementResults[x.Parent()]; !ok {
				pc.maybeFail(x.Pos(), "unproven owner of returned guarded element")
			}
			continue
		case *ssa.BinOp:
			if x.Op == token.EQL || x.Op == token.NEQ {
				continue
			}
		case *ssa.Store:
			if x.Val == v {
				pc.maybeFail(x.Pos(), "guarded element address escapes into storage")
				continue
			}
		}
		var guard fieldGuardResolver
		for _, g := range pc.elementFields[origin.field].GuardedBy {
			guard = g
		}
		lock, held := ls.isHeld(guard.resolveField(pc, ls, origin.owner), true)
		if !held {
			pc.maybeFail(inst.Pos(), "guarded element requires original lock %s", lock)
			continue
		}
		switch x := inst.(type) {
		case *ssa.Store, *ssa.UnOp:
		case *ssa.Call:
			pc.checkElementCall(x, v, lock, ls)
		default:
			pc.maybeFail(inst.Pos(), "unproven use of guarded element address")
		}
	}
}
