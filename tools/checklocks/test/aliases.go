// Copyright 2021 The gVisor Authors.
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

package test

import (
	"sync"

	"gvisor.dev/gvisor/tools/checklocks/test/crosspkg"
)

// +checklocksalias:inner.mu=mu
type aliasOuter struct {
	mu    sync.Mutex
	inner aliasInner

	// +checklocks:inner.mu
	guardedField int
}

func testTypeAliasValid(a *aliasOuter) {
	a.mu.Lock()
	a.guardedField = 1
	a.mu.Unlock()
}

func testTypeAliasInvalid(a *aliasOuter) {
	a.guardedField = 1 // +checklocksfail
}

type aliasInner struct {
	mu *sync.Mutex
}

// +checklocksalias:inner.mu=mu
type aliasInnerNested struct {
	mu    sync.Mutex
	inner aliasInner
}

// +checklocksalias:inner.mu=mu
// +checklocksalias:inner.mu=mu
type aliasDuplicateExact struct { // +checklocksfail=is redundant
	mu    sync.Mutex
	inner aliasInner
}

// +checklocksalias:inner.mu=mu
// +checklocksalias:mu=inner.mu
type aliasDuplicateSwapped struct { // +checklocksfail=is redundant
	mu    sync.Mutex
	inner aliasInner
}

// +checklocksalias:inner.mu=mu
type aliasNonStructField struct { // +checklocksfail=expected to be struct
	mu    sync.Mutex
	inner int
}

// +checklocksalias:mu=mu
type aliasSameLock struct { // +checklocksfail=refers to the same lock
	mu sync.Mutex
}

// +checklocksalias:mu=inner.mu
type aliasRequiresPointer struct { // +checklocksfail=requires a pointer or interface lock
	mu    sync.Mutex
	inner struct{ mu sync.Mutex }
}

// +checklocksalias:mu
type aliasInvalidFormat struct { // +checklocksfail=invalid annotation
	mu sync.Mutex
}

// +checklocksalias:mu=other.mu
type aliasNonStructType int // +checklocksfail=only valid on struct types

// +checklocksalias:inner.inner.mu=inner.mu
type aliasRedundantNested struct { // +checklocksfail=is redundant
	inner aliasInnerNested
}

type aliasEndpoint struct {
	mu sync.Mutex
}

type aliasSender struct {
	ep *aliasEndpoint
}

// +checklocksalias:snd.ep.mu=ep.mu
type aliasReceiver struct {
	ep  *aliasEndpoint
	snd aliasSender
}

// +checklocksalias:rc.snd.ep.mu=rc.ep.mu
type aliasRedundantDeep struct { // +checklocksfail=is redundant
	rc aliasReceiver
}

// +checklocksalias:inner.mu=mu
type aliasRWOuter struct {
	mu    sync.RWMutex
	inner struct{ mu *sync.RWMutex }
}

// +checklocks:mu
func aliasRequireWrite(mu *sync.RWMutex) {}

// +checklocksread:mu
func aliasRequireRead(mu *sync.RWMutex) {}

// A read lock must retain its mode when reached through a type alias.
// +checklocksread:a.mu
func testAliasReadMode(a *aliasRWOuter) {
	aliasRequireRead(a.inner.mu)
	aliasRequireWrite(a.inner.mu) // +checklocksfail=must hold mu exclusively
}

// +checklocks:a.mu
func testAliasWriteMode(a *aliasRWOuter) {
	aliasRequireRead(a.inner.mu)
	aliasRequireWrite(a.inner.mu)
}

func testAliasUnlocked(a *aliasRWOuter) {
	aliasRequireRead(a.inner.mu) // +checklocksfail=must hold mu non-exclusively
}

// +checklocksalias:inner.mu=mu
type aliasBranchOuter struct {
	mu sync.RWMutex

	// +checklocks:mu
	inner *aliasBranchInner
}

// +checklocksalias:mu=outer.mu
type aliasBranchInner struct {
	mu    *sync.RWMutex
	outer *aliasBranchOuter
}

// Visiting the inner type discovers the reverse alias, which can change the
// held mutex's representative without changing its identity.
func visitAliasBranchInner(inner *aliasBranchInner) {}

// +checklocks:a.mu
// +checklocks:b.mu
func testAliasBranchSameLock(a, b *aliasBranchOuter, visit bool) {
	if !visit {
		return
	}
	visitAliasBranchInner(a.inner)
}

// The negative cases return values so exit diagnostics have positions distinct
// from the incompatible-state diagnostic at the function declaration.
//
// +checklocks:a.mu
func testAliasBranchWrongMode(a *aliasBranchOuter, visit bool) bool { // +checklocksfail=incompatible return states
	if !visit {
		return false
	}
	visitAliasBranchInner(a.inner)
	a.mu.Unlock()
	a.mu.RLock()
	return true // +checklocksfail=not held exclusively
}

// +checklocks:a.mu
// +checklocks:b.mu
func testAliasBranchWrongOwner(a, b, c *aliasBranchOuter, visit bool) bool { // +checklocksfail=incompatible return states
	if !visit {
		return false
	}
	visitAliasBranchInner(a.inner)
	b.mu.Unlock()
	c.mu.Lock()
	return true // +checklocksfail=not held exclusively
}

// +checklocks:a.mu
func testAliasBranchExtraLock(a, b *aliasBranchOuter, visit bool) bool { // +checklocksfail=incompatible return states
	if !visit {
		return false
	}
	visitAliasBranchInner(a.inner)
	b.mu.Lock()
	return true // +checklocksfail=return with unexpected locks held
}

type returnedAliasOwner struct {
	mu     sync.RWMutex
	member returnedAliasMember
}

type returnedAliasMember struct {
	mu *sync.RWMutex

	// +checklocks:mu
	value int
}

// +checklocksalias:owner.mu=member.mu
func (owner *returnedAliasOwner) memberHandle() (member *returnedAliasMember) {
	return &owner.member
}

// +checklocksalias:member.mu=owner.mu
func (owner *returnedAliasOwner) tupleHandle() (member *returnedAliasMember, ok bool) {
	return &owner.member, true
}

func testReturnedAlias(owner, other *returnedAliasOwner) {
	member := owner.memberHandle()
	member.value++ // +checklocksfail=invalid field access|invalid field access
	owner.mu.Lock()
	member.value++
	owner.mu.Unlock()
	member.value++ // +checklocksfail=invalid field access|invalid field access
	other.mu.Lock()
	member.value++ // +checklocksfail=invalid field access|invalid field access
	other.mu.Unlock()
	owner.mu.Lock()
	member.value++ // The stable handle remains valid after relocking.
	owner.mu.Unlock()
}

func testReturnedAliasReadMode(owner *returnedAliasOwner) {
	owner.mu.RLock()
	member := owner.memberHandle()
	_ = member.value
	member.value++ // +checklocksfail=invalid field access
	owner.mu.RUnlock()
}

func testReturnedTupleAlias(owner *returnedAliasOwner) {
	owner.mu.Lock()
	member, _ := owner.tupleHandle()
	member.value++
	owner.mu.Unlock()
	member.value++ // +checklocksfail=invalid field access|invalid field access
}

// An unused returned lock is not an acquisition that must be balanced.
func testReturnedAliasUnused(owner *returnedAliasOwner) {
	owner.memberHandle()
	owner.tupleHandle()
	defer owner.memberHandle()
	defer owner.tupleHandle()
}

// +checklocksalias:a.mu=b.mu
func testReturnedAliasParameters(a, b *returnedAliasOwner) {} // +checklocksfail=requires one parameter lock and one returned lock

// +checklocksalias:owner.mu
func testReturnedAliasMalformed(owner *returnedAliasOwner) {} // +checklocksfail=invalid annotation

// +checklocksalias:owner.mu=member.mu
// +checklocksalias:member.mu=owner.mu
func testReturnedAliasDuplicate(owner *returnedAliasOwner) (member *returnedAliasMember) { // +checklocksfail=is redundant
	return &owner.member
}

func testImportedReturnedAlias(owner, other *crosspkg.ReturnAliasOwner) {
	member := owner.Member()
	member.Modify() // +checklocksfail=must hold
	other.Mu.Lock()
	member.Modify() // +checklocksfail=must hold
	other.Mu.Unlock()
	owner.Mu.Lock()
	member.Modify()
	owner.Mu.Unlock()
	owner.Mu.Lock()
	member.Modify()
	owner.Mu.Unlock()
}

type returnedValueMutex struct {
	mu sync.Mutex
}

// +checklocksalias:owner.mu=member.mu
func testReturnedValueMutexAlias(owner *aliasOuter) (member *returnedValueMutex) { // +checklocksfail=requires a pointer or interface lock
	return nil
}

// +checklocksalias:mu=result
func returnedMutexIdentity(mu *sync.Mutex) (result *sync.Mutex) {
	return mu
}

func testBareReturnedMutexAlias(mu *sync.Mutex) {
	mu.Lock()
	returnedMutexIdentity(mu).Unlock()
}
