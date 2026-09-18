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

	"golang.org/x/tools/go/ssa"
)

// iteratorFacts records positive proofs for iterator functions and constructor
// results. Anonymous functions have no exportable object, so a constructor's
// facts carry the proof for each returned iterator instead.
type iteratorFacts struct {
	// Transparent means that yield is called synchronously with the iterator's
	// incoming lock state, and that the iterator preserves that state.
	Transparent bool

	// Results identifies transparent iterators returned by this function.
	Results []bool
}

func (*iteratorFacts) AFact() {}

// iteratorSignature identifies func(func(...) bool), including named types.
func iteratorSignature(typ types.Type) bool {
	sig, ok := typ.Underlying().(*types.Signature)
	if !ok || sig.Params().Len() != 1 || sig.Results().Len() != 0 {
		return false
	}
	yield, ok := sig.Params().At(0).Type().Underlying().(*types.Signature)
	return ok && yield.Results().Len() == 1 &&
		types.Identical(yield.Results().At(0).Type(), types.Typ[types.Bool])
}

// iteratorFunctionFacts computes local proofs, or imports a dependency's proof.
// Installing the empty entry first makes recursive proofs fail closed.
func (pc *passContext) iteratorFunctionFacts(fn *ssa.Function) *iteratorFacts {
	if facts, ok := pc.iterators[fn]; ok {
		return facts
	}
	facts := new(iteratorFacts)
	pc.iterators[fn] = facts
	obj, _ := fn.Object().(*types.Func)
	if len(fn.Blocks) == 0 {
		if obj != nil {
			pc.pass.ImportObjectFact(originObject(obj), facts)
		}
		return facts
	}

	if obj != nil {
		var contract lockFunctionFacts
		pc.importLockFunctionFacts(obj, &contract)
		if contract.Ignore {
			return facts
		}
	}
	facts.Transparent = pc.transparentIterator(fn)
	facts.Results = make([]bool, fn.Signature.Results().Len())
	for i := range facts.Results {
		if !iteratorSignature(fn.Signature.Results().At(i).Type()) {
			continue
		}
		found, transparent := false, true
		for _, block := range fn.Blocks {
			for _, inst := range block.Instrs {
				if ret, ok := inst.(*ssa.Return); ok {
					found = true
					transparent = transparent && pc.transparentIteratorValue(ret.Results[i], make(map[ssa.Value]bool))
				}
			}
		}
		facts.Results[i] = found && transparent
	}

	if obj != nil && obj.Pkg() == pc.pass.Pkg {
		useful := facts.Transparent
		for _, result := range facts.Results {
			useful = useful || result
		}
		if useful {
			pc.pass.ExportObjectFact(originObject(obj), facts)
		}
	}
	return facts
}

// transparentIteratorValue follows constructor results without treating an
// unannotated constructor as evidence about the callable that it returns.
func (pc *passContext) transparentIteratorValue(v ssa.Value, active map[ssa.Value]bool) bool {
	if active[v] {
		return false
	}
	active[v] = true
	defer delete(active, v)
	switch v := v.(type) {
	case *ssa.Function:
		return pc.iteratorFunctionFacts(v).Transparent
	case *ssa.MakeClosure:
		fn := v.Fn.(*ssa.Function)
		return pc.iteratorFunctionFacts(fn).Transparent && privateIteratorCaptures(v)
	case *ssa.ChangeType:
		return pc.transparentIteratorValue(v.X, active)
	case *ssa.Phi:
		if len(v.Edges) == 0 {
			return false
		}
		for _, edge := range v.Edges {
			if !pc.transparentIteratorValue(edge, active) {
				return false
			}
		}
		return true
	case *ssa.Call:
		return pc.transparentIteratorResult(v, 0)
	case *ssa.Extract:
		if call, ok := v.Tuple.(*ssa.Call); ok {
			return pc.transparentIteratorResult(call, v.Index)
		}
	}
	return false
}

func (pc *passContext) transparentIteratorResult(call *ssa.Call, result int) bool {
	fn := call.Common().StaticCallee()
	if fn == nil {
		return false
	}
	facts := pc.iteratorFunctionFacts(fn)
	return result < len(facts.Results) && facts.Results[result]
}

// emptyLockContract excludes preconditions as well as net lock changes: a
// returned callable must not hide requirements that its caller cannot check.
func (pc *passContext) emptyLockContract(fn *types.Func) bool {
	var facts lockFunctionFacts
	pc.importLockFunctionFacts(fn, &facts)
	return !facts.Ignore && len(facts.HeldOnEntry) == 0 &&
		len(facts.HeldOnExit) == 0 && len(facts.ExcludedOnEntry) == 0
}

// transparentIterator proves a restricted callback contract from source. A
// completed ordinary static call uses its normal checklocks contract; this is
// not a claim that such a helper never temporarily changes its own locks. The
// distinction is safe here because helpers cannot receive yield unless they
// independently have the same positive callback proof.
func (pc *passContext) transparentIterator(fn *ssa.Function) bool {
	if !iteratorSignature(fn.Signature) || len(fn.Params) != 1 {
		return false
	}
	if obj, ok := fn.Object().(*types.Func); ok && !pc.emptyLockContract(obj) {
		return false
	}
	yield := fn.Params[0]
	for _, block := range fn.Blocks {
		for _, inst := range block.Instrs {
			if _, call := inst.(*ssa.Call); !call && pc.changesLockValue(inst) {
				return false
			}
			switch inst := inst.(type) {
			case *ssa.Store:
				// Fresh locals are private. A directly captured scalar
				// cell is permitted only when the MakeClosure also proves
				// ownership; indirect/global stores may affect the caller.
				if _, captured := inst.Addr.(*ssa.FreeVar); !captured && !freshAlloc(inst.Addr) {
					return false
				}
			case *ssa.Go, *ssa.Defer, *ssa.Send, *ssa.MapUpdate:
				return false
			case *ssa.Call:
				call := inst.Common()
				if call.Value == yield {
					continue
				}
				passesYield := false
				for _, arg := range call.Args {
					passesYield = passesYield || arg == yield
				}
				if passesYield {
					if len(call.Args) != 1 || !pc.transparentIteratorValue(call.Value, make(map[ssa.Value]bool)) {
						return false
					}
					continue
				}
				if lockOperation(call) || pc.callMayChangeLockIdentity(call) {
					return false
				}
				if builtin, ok := call.Value.(*ssa.Builtin); ok {
					switch builtin.Name() {
					case "append", "copy", "clear", "delete":
						return false
					}
					continue
				}
				for _, arg := range call.Args {
					if mutableReference(arg.Type()) {
						return false
					}
				}
				callee := call.StaticCallee()
				if callee == nil {
					return false
				}
				obj, ok := callee.Object().(*types.Func)
				if !ok || !pc.emptyLockContract(obj) {
					return false
				}
			}
		}
	}

	// Calls above are the only permitted uses of yield. In particular it must
	// not be stored, returned, captured, deferred, or passed to unknown code.
	if refs := yield.Referrers(); refs != nil {
		for _, ref := range *refs {
			switch ref := ref.(type) {
			case *ssa.DebugRef:
				continue
			case *ssa.Call:
				if ref.Common().Value == yield || len(ref.Common().Args) == 1 && ref.Common().Args[0] == yield {
					continue
				}
			}
			return false
		}
	}
	return true
}

// lockValue identifies storage whose mutation can change which mutex a later
// iteration or the enclosing function refers to. Interfaces and unsafe pointer
// representations are conservative because their concrete target is unknown.
func (pc *passContext) lockValue(typ types.Type, seen map[types.Type]bool) bool {
	if seen[typ] {
		return false
	}
	seen[typ] = true
	if named, ok := types.Unalias(typ).(*types.Named); ok && mutexRE.MatchString(named.Obj().Name()) {
		return true
	}
	switch typ := typ.Underlying().(type) {
	case *types.Basic:
		return typ.Kind() == types.UnsafePointer || typ.Kind() == types.Uintptr
	case *types.Interface, *types.Signature:
		return true
	case *types.Pointer:
		return pc.lockValue(typ.Elem(), seen)
	case *types.Array:
		return pc.lockValue(typ.Elem(), seen)
	case *types.Slice:
		return pc.lockValue(typ.Elem(), seen)
	case *types.Chan:
		return pc.lockValue(typ.Elem(), seen)
	case *types.Map:
		return pc.lockValue(typ.Key(), seen) || pc.lockValue(typ.Elem(), seen)
	case *types.Struct:
		for i := 0; i < typ.NumFields(); i++ {
			field := typ.Field(i)
			var facts lockGuardFacts
			pc.importLockGuardFacts(field, &facts)
			if len(facts.GuardedBy) != 0 || pc.lockValue(field.Type(), seen) {
				return true
			}
		}
	}
	return false
}

func (pc *passContext) changesLockValue(inst ssa.Instruction) bool {
	var typ types.Type
	switch inst := inst.(type) {
	case *ssa.Store:
		typ = inst.Val.Type()
	case *ssa.MapUpdate:
		typ = inst.Value.Type()
	case *ssa.Call:
		if pc.callMayChangeLockIdentity(inst.Common()) {
			return true
		}
	}
	return typ != nil && pc.lockValue(typ, make(map[types.Type]bool))
}

// lockOperation matches the same intrinsic receiver and operation rules used
// by checkFunctionCall. Their effects are checked as lock-state changes, not
// unknown mutations of the receiver's identity.
func lockOperation(call *ssa.CallCommon) bool {
	obj := call.Method
	if obj == nil {
		if fn := call.StaticCallee(); fn != nil {
			obj, _ = fn.Object().(*types.Func)
		}
	}
	if obj == nil || !lockerRE.MatchString(obj.FullName()) && !mutexRE.MatchString(obj.FullName()) {
		return false
	}
	switch obj.Name() {
	case "Lock", "NestedLock", "RLock", "Unlock", "NestedUnlock", "RUnlock", "DowngradeLock":
		return true
	}
	return false
}

// callMayChangeLockIdentity is conservative about memory effects absent from
// normal lock contracts. In particular swap(&p) can change which p.mu is held
// even without acquiring or releasing a lock. Direct anonymous calls are
// analyzed inline, so their individual writes receive the same checks.
func (pc *passContext) callMayChangeLockIdentity(call *ssa.CallCommon) bool {
	if len(call.Args) == 1 {
		if closure, ok := call.Args[0].(*ssa.MakeClosure); ok && rangeYield(closure.Fn.(*ssa.Function)) {
			return false // The nested loop has its own entry and return checks.
		}
	}
	if lockOperation(call) {
		return false
	}
	if builtin, ok := call.Value.(*ssa.Builtin); ok {
		switch builtin.Name() {
		case "append", "copy", "clear", "delete":
			return pc.lockValue(call.Args[0].Type(), make(map[types.Type]bool))
		}
		return false
	}
	fn := call.StaticCallee()
	if fn == nil {
		return true
	}
	obj, named := fn.Object().(*types.Func)
	if !named {
		return false // checkCall analyzes this synchronous anonymous function.
	}
	var facts lockFunctionFacts
	pc.importLockFunctionFacts(obj, &facts)
	if facts.Ignore {
		return true
	}
	for _, arg := range call.Args {
		if pc.lockValue(arg.Type(), make(map[types.Type]bool)) {
			return true
		}
	}
	return false
}

// privateIteratorCaptures permits mutable captured cells only when their
// allocation is owned by the constructor and has no other users. SplitSeq's
// private string cell qualifies; a caller's shared scalar selector does not.
func privateIteratorCaptures(closure *ssa.MakeClosure) bool {
	fn := closure.Fn.(*ssa.Function)
	for _, block := range fn.Blocks {
		for _, inst := range block.Instrs {
			store, ok := inst.(*ssa.Store)
			if !ok {
				continue
			}
			fv, captured := store.Addr.(*ssa.FreeVar)
			if !captured {
				continue
			}
			for i, capture := range fn.FreeVars {
				if capture != fv {
					continue
				}
				alloc, owned := closure.Bindings[i].(*ssa.Alloc)
				if !owned || alloc.Parent() != closure.Parent() {
					return false
				}
				if refs := alloc.Referrers(); refs != nil {
					for _, ref := range *refs {
						if ref == closure {
							continue
						}
						switch ref := ref.(type) {
						case *ssa.DebugRef:
							continue
						case *ssa.Store:
							if ref.Addr == alloc {
								continue
							}
						}
						return false
					}
				}
			}
		}
	}
	return true
}

func mutableReference(typ types.Type) bool {
	switch typ.Underlying().(type) {
	case *types.Pointer, *types.Slice, *types.Map, *types.Chan, *types.Interface, *types.Signature:
		return true
	}
	return false
}

// lockDependencies preserves the addresses read to select a held mutex,
// independently of the identities evaluated for those reads. Thus idx=0 does
// not erase the dependency on idx, and globals and distinct FieldAddr nodes
// for the same location agree without SSA Referrers or string containment.
func (l *lockState) lockDependencies(rv resolvedValue) map[string]struct{} {
	dependencies := make(map[string]struct{})
	seen := make(map[ssa.Value]bool)
	var address func(ssa.Value)
	address = func(v ssa.Value) {
		v = l.bound(v)
		key, _ := l.valueAndObject(v)
		dependencies[key] = struct{}{}
		// A helper receiving the containing object can mutate this field
		// or element even when it does not receive the exact leaf address.
		switch v := v.(type) {
		case *ssa.FieldAddr:
			address(v.X)
		case *ssa.IndexAddr:
			address(v.X)
		}
	}
	var visit func(ssa.Value)
	visit = func(v ssa.Value) {
		if v == nil || seen[v] {
			return
		}
		seen[v] = true
		if bound := l.bound(v); bound != v {
			visit(bound)
		}
		switch v := v.(type) {
		case *ssa.UnOp:
			if v.Op == token.MUL {
				address(v.X)
			}
		case *ssa.Lookup:
			address(v.X)
		}
		if inst, ok := v.(ssa.Instruction); ok {
			for _, operand := range inst.Operands(nil) {
				if operand != nil {
					visit(*operand)
				}
			}
		}
	}
	visit(rv.value)
	return dependencies
}

func (l *lockState) affectsLockIdentity(addr ssa.Value) bool {
	key, _ := l.valueAndObject(addr)
	key = l.aliasKey(key)
	for _, lock := range l.lockedMutexes {
		for dependency := range lock.dependencies {
			if l.aliasKey(dependency) == key {
				return true
			}
		}
	}
	// An unknown index may alias another element in the same storage.
	if indexed, ok := l.bound(addr).(*ssa.IndexAddr); ok {
		return l.affectsLockIdentity(indexed.X)
	}
	return false
}

// changesRangeLockIdentity checks visible writes and mutable arguments against
// held-lock dependencies. Ordinary scalar counters remain supported; bump(&idx)
// is rejected when idx selected a held mutex, even though *int contains no lock.
func (pc *passContext) changesRangeLockIdentity(inst ssa.Instruction, ls *lockState) bool {
	if pc.changesLockValue(inst) {
		return true
	}
	switch inst := inst.(type) {
	case *ssa.Store:
		return ls.affectsLockIdentity(inst.Addr)
	case *ssa.MapUpdate:
		return ls.affectsLockIdentity(inst.Map)
	case *ssa.Call:
		call := inst.Common()
		if lockOperation(call) {
			return false
		}
		if builtin, ok := call.Value.(*ssa.Builtin); ok {
			switch builtin.Name() {
			case "append", "copy", "clear", "delete":
				return ls.affectsLockIdentity(call.Args[0])
			}
			return false
		}
		if callee := call.StaticCallee(); callee != nil && callee.Object() == nil {
			return false // Its writes are checked during inline analysis.
		}
		for _, arg := range call.Args {
			if mutableReference(arg.Type()) && ls.affectsLockIdentity(arg) {
				return true
			}
		}
	}
	return false
}

func rangeYield(fn *ssa.Function) bool {
	return fn.Synthetic == "range-over-func yield"
}

// checkRangeCall checks the synthetic body at the iterator invocation, rather
// than at closure creation. A positive proof is required to borrow caller locks.
func (pc *passContext) checkRangeCall(call callCommon, lff *lockFunctionFacts, ls *lockState) bool {
	if len(call.Common().Args) != 1 {
		return false
	}
	closure, ok := call.Common().Args[0].(*ssa.MakeClosure)
	if !ok || !rangeYield(closure.Fn.(*ssa.Function)) {
		return false
	}
	fn := closure.Fn.(*ssa.Function)
	// Preserve ordinary named-call contract checking even when the iterator
	// is not transparent, for example a named iterator with a required lock.
	if callee := call.Common().StaticCallee(); callee != nil {
		if obj, ok := callee.Object().(*types.Func); ok {
			var facts lockFunctionFacts
			pc.importLockFunctionFacts(obj, &facts)
			facts.Ignore = facts.Ignore || lff.Ignore
			pc.checkFunctionCall(call, obj, &facts, ls)
		}
	}

	transparent := pc.transparentIteratorValue(ls.bound(call.Common().Value), make(map[ssa.Value]bool))
	entry := ls.fork()
	if !transparent {
		entry.lockedMutexes = make(map[string]lockInfo)
	}
	for i, fv := range fn.FreeVars {
		entry.bind(fv, closure.Bindings[i])
	}

	for _, block := range fn.Blocks {
		for _, inst := range block.Instrs {
			if _, ok := inst.(*ssa.Defer); ok {
				if !lff.Ignore {
					pc.maybeFail(inst.Pos(), "defer in a range-over-function body is not supported")
				}
				// These defers belong to the enclosing function, not to
				// the synthetic callback's ordinary return sequence.
				pc.functions[fn] = struct{}{}
				return true
			}
		}
	}

	previous, nested := pc.rangeEntries[fn]
	pc.rangeEntries[fn] = entry
	pc.rangeDepth++
	defer func() {
		pc.rangeDepth--
		if nested {
			pc.rangeEntries[fn] = previous
		} else {
			delete(pc.rangeEntries, fn)
		}
	}()
	facts := lockFunctionFacts{Ignore: lff.Ignore}
	exit := pc.checkFunction(nil, fn, &facts, entry, true /* force */)
	if exit != nil {
		if transparent {
			// An iterator may yield zero or multiple times. The mutation
			// checks above keep lock identities stable across iterations;
			// retain only storage/alias facts valid on the zero-yield path
			// as well as after the body. Never import its defer stack.
			merged := ls.fork()
			merged.intersect(exit)
			ls.returnFrom(merged)
		}
	}
	return true
}

// checkRangeReturn checks every path before return states are intersected. A
// conditional extra lock must not disappear in the join with an unlocked path.
func (pc *passContext) checkRangeReturn(fn *ssa.Function, lff *lockFunctionFacts, ls *lockState) {
	if entry, ok := pc.rangeEntries[fn]; ok && !entry.isCompatible(ls) && !lff.Ignore {
		pc.maybeFail(fn.Pos(), "range body changes lock state")
	}
}
