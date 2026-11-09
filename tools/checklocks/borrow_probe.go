// Copyright 2020 The gVisor Authors.
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

// borrowLeaf admits a one-argument leaf that only reads fields or clears
// fields of its argument. A pointer it returns is derived from that argument.
func borrowLeaf(c *ssa.Call) bool {
	f := c.Common().StaticCallee()
	if f == nil || len(f.Params) != 1 || len(f.Blocks) != 1 {
		return false
	}
	derived := map[ssa.Value]bool{f.Params[0]: true}
	for _, inst := range f.Blocks[0].Instrs {
		switch x := inst.(type) {
		case *ssa.FieldAddr:
			derived[x] = derived[x.X]
		case *ssa.UnOp:
			if x.Op != token.MUL || !derived[x.X] {
				return false
			}
			derived[x] = true
		case *ssa.Store:
			if _, constant := x.Val.(*ssa.Const); !constant || !derived[x.Addr] {
				return false
			}
		case *ssa.Return:
			return len(x.Results) == 0 || (len(x.Results) == 1 && derived[x.Results[0]])
		case *ssa.DebugRef:
		default:
			return false
		}
	}
	return false
}

// probeBorrow checks the caller of an explicitly annotated pointer-returning
// producer. This experiment trusts the producer's ownership declaration.
func (pc *passContext) probeBorrow(origin *ssa.Call, owner string, ls *lockState) {
	if _, ok := origin.Type().Underlying().(*types.Pointer); !ok {
		pc.maybeFail(origin.Pos(), "borrow probe: unsupported result type")
		return
	}
	var enclosing lockFunctionFacts
	if fn, ok := origin.Parent().Object().(*types.Func); ok {
		pc.importLockFunctionFacts(fn, &enclosing)
	}
	forward := false
	if fg, ok := enclosing.HeldOnEntry[enclosing.BorrowedResult]; ok {
		r := fg.Resolver.resolveStatic(pc, ls, origin.Parent(), nil)
		if r.valid() {
			key, _ := ls.isHeld(r, fg.Exclusive)
			forward = key == owner
		}
	}

	uses := make(map[ssa.Instruction]string)
	seenValues := make(map[ssa.Value]bool)
	values := []ssa.Value{origin}
	for len(values) != 0 {
		v := values[0]
		values = values[1:]
		if seenValues[v] {
			continue
		}
		seenValues[v] = true
		for _, use := range *v.Referrers() {
			if _, ok := uses[use]; !ok {
				uses[use] = ""
			}
			switch x := use.(type) {
			case *ssa.FieldAddr, *ssa.IndexAddr, *ssa.ChangeType:
				values = append(values, use.(ssa.Value))
			case *ssa.UnOp:
				if x.Op == token.MUL {
					switch x.Type().Underlying().(type) {
					case *types.Pointer:
						values = append(values, x)
						continue
					case *types.Basic:
						continue
					}
				}
				uses[x] = "unproven load"
			case *ssa.Store:
				if x.Val == v {
					uses[x] = "stored pointer"
				}
			case *ssa.BinOp:
				if x.Op != token.EQL && x.Op != token.NEQ {
					uses[x] = "unproven operation"
				}
			case *ssa.Call:
				if !borrowLeaf(x) {
					uses[x] = "unproven callee"
				} else if _, ptr := x.Type().Underlying().(*types.Pointer); ptr {
					values = append(values, x)
				}
			case *ssa.Return:
				if !forward {
					uses[x] = "returned without same-owner contract"
				}
			case *ssa.DebugRef:
			default:
				uses[use] = "unproven escape"
			}
		}
	}

	type point struct {
		block           *ssa.BasicBlock
		start           int
		mayHaveUnlocked bool
	}
	block := origin.Block()
	work := []point{{block, slices.Index(block.Instrs, ssa.Instruction(origin)) + 1, false}}
	seen := make(map[point]bool)
	for len(work) != 0 {
		p := work[0]
		work = work[1:]
		if seen[p] {
			continue
		}
		seen[p] = true
		end := true
		for _, inst := range p.block.Instrs[p.start:] {
			if inst == origin {
				end = false // The next loop iteration gets a fresh result.
				break
			}
			if reason, used := uses[inst]; used {
				if reason != "" {
					pc.maybeFail(inst.Pos(), "borrow probe: %s", reason)
				} else if p.mayHaveUnlocked {
					pc.maybeFail(inst.Pos(), "borrow probe: possible intervening release")
				}
			}
			switch x := inst.(type) {
			case *ssa.Call:
				p.mayHaveUnlocked = p.mayHaveUnlocked || !borrowLeaf(x)
			case *ssa.Go, *ssa.RunDefers:
				p.mayHaveUnlocked = true
			}
		}
		if end {
			for _, next := range p.block.Succs {
				work = append(work, point{next, 0, p.mayHaveUnlocked})
			}
		}
	}
}
