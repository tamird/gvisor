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

// A callbackSlot is the index of the only function parameter a helper calls.
// Apart from invoking or forwarding that parameter, the helper only reads
// data. In particular it cannot save the callback or release a caller's lock.
type callbackSlot int

func (*callbackSlot) AFact() {}

// callbackSlot follows the generic declaration: importers need its contract,
// not an SSA body for a particular instantiation. Only ordinary package-level
// functions own these facts; a generated wrapper may share a method's object.
func (pc *passContext) callbackSlot(fn *ssa.Function) int {
	if fn == nil {
		return -1
	}
	if origin := fn.Origin(); origin != nil {
		fn = origin
	}
	obj, ok := fn.Object().(*types.Func)
	if !ok || fn.Pkg == nil || fn.Pkg.Members[obj.Name()] != fn {
		return -1
	}
	obj = obj.Origin()
	if obj.Pkg() != pc.pass.Pkg {
		var slot callbackSlot
		if pc.pass.ImportObjectFact(obj, &slot) {
			return int(slot)
		}
		return -1
	}
	if slot, ok := pc.callbackSlots[fn]; ok {
		return slot
	}
	for i, param := range fn.Params {
		if _, ok := param.Type().Underlying().(*types.Signature); !ok {
			continue
		}
		if pc.callbackSlots == nil {
			pc.callbackSlots = make(map[*ssa.Function]int)
		}
		// Reject recursive forwarding rather than assuming a cycle is safe.
		pc.callbackSlots[fn] = -1
		if pc.onlyCalls(fn, param) {
			pc.callbackSlots[fn] = i
			slot := callbackSlot(i)
			pc.pass.ExportObjectFact(obj, &slot)
			return i
		}
	}
	return -1
}

// onlyCalls accepts direct calls to param and calls that pass it to a
// previously proved slot. Even a completely synchronous call to another
// function value could change which locks are held before param runs.
func (pc *passContext) onlyCalls(fn *ssa.Function, param *ssa.Parameter) bool {
	called := false
	for _, block := range fn.Blocks {
		for _, inst := range block.Instrs {
			switch x := inst.(type) {
			case *ssa.Call:
				c := x.Common()
				n := slices.Index(c.Args, ssa.Value(param))
				if n >= 0 {
					if n != pc.callbackSlot(c.StaticCallee()) || slices.Contains(c.Args[n+1:], ssa.Value(param)) {
						return false
					}
				} else if c.Value != param {
					if b, ok := c.Value.(*ssa.Builtin); ok && b.Name() == "len" {
						continue
					}
					return false
				}
				called = true
				continue
			case *ssa.UnOp:
				if x.Op != token.MUL {
					return false
				}
			case *ssa.BinOp, *ssa.ChangeType, *ssa.Convert, *ssa.Extract,
				*ssa.Field, *ssa.FieldAddr, *ssa.If, *ssa.Index, *ssa.IndexAddr,
				*ssa.Jump, *ssa.Phi, *ssa.Return:
			default:
				// Includes stores, goroutines, defers and unknown instructions.
				return false
			}
			for _, operand := range inst.Operands(nil) {
				if *operand == param {
					return false
				}
			}
		}
	}
	return called
}

// callback returns a closure or bound method that can borrow locks at call.
// Callback parameters need not be related to any argument of the helper.
func (pc *passContext) callback(call *ssa.Call) ssa.Value {
	for i, arg := range call.Common().Args {
		switch f := arg.(type) {
		case *ssa.MakeClosure:
		case *ssa.Function:
			if f.Object() != nil {
				continue
			}
		default:
			continue
		}
		if pc.callbackSlot(call.Common().StaticCallee()) == i &&
			!slices.Contains(call.Common().Args[:i], arg) && !slices.Contains(call.Common().Args[i+1:], arg) {
			return arg
		}
	}
	return nil
}

// checkCallback joins zero invocations with each possible subsequent one.
// For example, a callback can reassign a captured pointer without changing
// any held locks. Neither the next invocation nor the caller can rely on the
// old pointer value. As with loop backedges, each intersection only drops facts.
func (pc *passContext) checkCallback(call *ssa.Call, callback ssa.Value, lff *lockFunctionFacts, ls *lockState) {
	previous := pc.callbackPos
	pc.callbackPos = call.Pos()
	defer func() { pc.callbackPos = previous }()
	for {
		var result *lockState
		switch f := callback.(type) {
		case *ssa.MakeClosure:
			result = pc.checkClosure(nil, f, lff, ls)
		case *ssa.Function:
			result = pc.checkFunction(nil, f, &lockFunctionFacts{Ignore: lff.Ignore}, ls, true)
		}
		if result == nil {
			return
		}
		if !ls.hasSameLocks(result) {
			if !lff.Ignore {
				pc.maybeFail(call.Pos(), "callback must preserve held locks (before: %s, after: %s)", ls.String(), result.String())
			}
			return
		}
		next := ls.fork()
		next.returnFrom(result)
		next.intersect(ls)
		if ls.equivalent(next) {
			return
		}
		*ls = *next
	}
}
