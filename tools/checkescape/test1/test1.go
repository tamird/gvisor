// Copyright 2019 The gVisor Authors.
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

// Package test1 is a test package.
package test1

import (
	"fmt"
)

// Interface is a generic interface.
type Interface interface {
	Foo()
}

// Type is a concrete implementation of Interface.
type Type struct {
	A uint64
	B uint64
}

// Foo implements Interface.Foo.
//
//go:nosplit
func (t Type) Foo() {
	fmt.Printf("%v", t) // Never executed.
}

// InterfaceFunction is passed an interface argument.
// +checkescape:all,hard
//
//go:nosplit
func InterfaceFunction(i Interface) {
	// Do nothing; exported for tests.
}

// TypeFunction is passed a concrete pointer argument.
// +checkescape:all,hard
//
//go:nosplit
func TypeFunction(t *Type) {
}

// BuiltinMap creates a new map.
// +mustescape:local,builtin
//
//go:noinline
//go:nosplit
func BuiltinMap(x int) map[string]bool {
	return make(map[string]bool)
}

// +mustescape:builtin
//
//go:noinline
//go:nosplit
func builtinMapRec(x int) map[string]bool {
	return BuiltinMap(x)
}

// BuiltinClosure returns a closure around x.
// +mustescape:local,builtin
//
//go:noinline
//go:nosplit
func BuiltinClosure(x int) func() {
	return func() {
		fmt.Printf("%v", x)
	}
}

// +mustescape:builtin
//
//go:noinline
//go:nosplit
func builtinClosureRec(x int) func() {
	return BuiltinClosure(x)
}

// BuiltinMakeSlice makes a new slice.
// +mustescape:local,builtin
//
//go:noinline
//go:nosplit
func BuiltinMakeSlice(x int) []byte {
	return make([]byte, x)
}

// +mustescape:builtin
//
//go:noinline
//go:nosplit
func builtinMakeSliceRec(x int) []byte {
	return BuiltinMakeSlice(x)
}

// BuiltinAppend calls append on a slice.
// +mustescape:local,builtin
//
//go:noinline
//go:nosplit
func BuiltinAppend(x []byte) []byte {
	return append(x, 0)
}

// +mustescape:builtin
//
//go:noinline
//go:nosplit
func builtinAppendRec() []byte {
	return BuiltinAppend(nil)
}

// BuiltinChan makes a channel.
// +mustescape:local,builtin
//
//go:noinline
//go:nosplit
func BuiltinChan() chan int {
	return make(chan int)
}

// +mustescape:builtin
//
//go:noinline
//go:nosplit
func builtinChanRec() chan int {
	return BuiltinChan()
}

// Heap performs an explicit heap allocation.
// +mustescape:local,heap
//
//go:noinline
//go:nosplit
func Heap() *Type {
	var t Type
	return &t
}

// +mustescape:heap
//
//go:noinline
//go:nosplit
func heapRec() *Type {
	return Heap()
}

// Dispatch dispatches via an interface.
// +mustescape:local,interface
//
//go:noinline
//go:nosplit
func Dispatch(i Interface) {
	i.Foo()
}

// +mustescape:interface
//
//go:noinline
//go:nosplit
func dispatchRec(i Interface) {
	Dispatch(i)
}

// Dynamic invokes a dynamic function.
// +mustescape:local,dynamic
//
//go:noinline
//go:nosplit
func Dynamic(f func()) {
	f()
}

// +mustescape:dynamic
//
//go:noinline
//go:nosplit
func dynamicRec(f func()) {
	Dynamic(f)
}

//go:noinline
//go:nosplit
func internalFunc() {
}

// Split includes a guaranteed stack split.
// +mustescape:local,stack
//
//go:noinline
func Split() {
	internalFunc()
}

// +mustescape:stack
//
//go:noinline
//go:nosplit
func splitRec() {
	Split()
}

// Callers precede their generic callees so local analysis cannot depend on
// facts having already been exported in declaration order.
// +checkescape:all
//
//go:noinline
//go:nosplit
func genericLocal(v int) int {
	return genericDelegate(v)
}

// +mustescape:heap
//
//go:noinline
//go:nosplit
func genericHeapLocal() *int {
	return genericAllocate[int]()
}

// +checkescape:all
//
//go:nosplit
func genericMethodExpressionLocal(v *Value[uint64, int]) (uint64, int) {
	return (*Value[uint64, int]).Get(v)
}

// Value holds a key and value for generic method calls.
type Value[K, V any] struct {
	Key  K
	Data V
}

// Get returns the stored key and value.
// +checkescape:all
//
//go:nosplit
func (v *Value[K, V]) Get() (K, V) {
	return v.Key, v.Data
}

// Copy allocates a value, without any instantiation in this package.
// +mustescape:local,heap
//
//go:noinline
//go:nosplit
func (v *Value[K, V]) Copy() *V {
	value := new(V)
	*value = v.Data
	return value
}

// +checkescape:all
//
//go:nosplit
func genericDelegate[T any](v T) T {
	return GenericIdentity(v)
}

// +mustescape:local,heap
//
//go:noinline
//go:nosplit
func genericAllocate[T any]() *T {
	return new(T)
}

// GenericIdentity returns its argument.
// +checkescape:all
//
//go:nosplit
func GenericIdentity[T any](v T) T {
	return v
}

// GenericHeap allocates a value, without any instantiation in this package.
// +mustescape:local,heap
//
//go:noinline
//go:nosplit
func GenericHeap[T any]() *T {
	return new(T)
}

// GenericScratch can need heap storage even though the local does not escape.
// +mustescape:local,heap
//
//go:noinline
//go:nosplit
func GenericScratch[T any](v T, i int) T {
	var values [2]T
	values[i] = v
	return values[0]
}

// GenericSplit needs a stack check, without any instantiation in this package.
// +mustescape:local,stack
//
//go:noinline
func GenericSplit[T any](v T) T {
	internalFunc()
	return v
}

// GenericClosure allocates from a closure within an uninstantiated function.
// +mustescape:heap
//
//go:noinline
//go:nosplit
func GenericClosure[T any]() *T {
	return func() *T {
		return new(T)
	}()
}

// GenericDispatch retains the uncertainty of a constrained method call.
// +mustescape:local,interface
//
//go:noinline
//go:nosplit
func GenericDispatch[T Interface](v T) {
	v.Foo()
}

// GenericDynamic retains the uncertainty of a callback.
// +mustescape:local,dynamic
//
//go:noinline
//go:nosplit
func GenericDynamic[T any](f func(T), v T) {
	f(v)
}

// GenericNumeric uses operations that are safe for its entire type set.
// +checkescape:all
//
//go:noinline
//go:nosplit
func GenericNumeric[T ~uint64](a, b T) (bool, uint32) {
	return a == b, uint32(a)
}

// GenericLookup searches a read-only collection with scalar keys.
// +checkescape:all
//
//go:noinline
//go:nosplit
func GenericLookup[K ~uint64 | ~string, V any](values []Value[K, V], key K) (V, bool) {
	for i := range values {
		if values[i].Key == key {
			return values[i].Data, true
		}
	}
	var zero V
	return zero, false
}

// GenericEqual may dispatch through an interface or aggregate equality helper.
// +mustescape:local,dynamic
//
//go:noinline
//go:nosplit
func GenericEqual[T comparable](a, b T) bool {
	return a == b
}

// GenericBox may allocate storage for the interface value.
// +mustescape:local,heap,dynamic
//
//go:noinline
//go:nosplit
func GenericBox[T any](v T) any {
	return v
}

// GenericBytes allocates writable storage for the string contents.
// +mustescape:local,heap,dynamic
//
//go:noinline
//go:nosplit
func GenericBytes[T ~string](v T) []byte {
	return []byte(v)
}

// GenericAssert may need runtime interface assertion helpers.
// +mustescape:local,dynamic
//
//go:noinline
//go:nosplit
func GenericAssert[T any](v any) (T, bool) {
	value, ok := v.(T)
	return value, ok
}

// GenericReceive calls the channel runtime.
// +mustescape:local,dynamic
//
//go:noinline
//go:nosplit
func GenericReceive[T any](ch <-chan T) T {
	return <-ch
}

// ConcreteReceive requires the same runtime call as GenericReceive.
// +mustescape:local,dynamic
//
//go:noinline
//go:nosplit
func ConcreteReceive(ch <-chan int) int {
	return <-ch
}

// ConcreteEqual may dispatch to an equality helper, like GenericEqual.
// +mustescape:local,dynamic
//
//go:noinline
//go:nosplit
func ConcreteEqual(a, b any) bool {
	return a == b
}

// ConcreteBox uses an explicit conversion with an SSA source position.
// +mustescape:local,heap,dynamic
//
//go:noinline
//go:nosplit
func ConcreteBox(v [4]uint64) any {
	return any(v)
}

// ConcreteBoxImplicit loses the conversion's source position in SSA. Its
// return is deliberately on a different line from the allocation.
// +mustescape:local,heap,dynamic
//
//go:noinline
//go:nosplit
func ConcreteBoxImplicit(v [4]uint64) any {
	var boxed any = v
	return boxed
}

// The compiler can eliminate these concrete operations even though their SSA
// instructions can require runtime calls for other types or uses.
// +checkescape:all
//
//go:noinline
//go:nosplit
func concreteEqualElided(a, b [1]uintptr) bool {
	return a == b
}

// +checkescape:all
//
//go:noinline
//go:nosplit
func concreteBoxElided(v int) int {
	var boxed any = v
	return boxed.(int)
}

// An unrelated non-escaping call and a stack split are not evidence of an
// implicit allocation. The call ensures that the compiler retains the split.
// +checkescape:hard,dynamic
// +mustescape:local,stack
//
//go:noinline
func concreteBoxElidedWithCall(v int) int {
	internalFunc()
	var boxed any = v
	return boxed.(int)
}

// Pointer boxing never needs storage, even with another call in the function.
// +checkescape:all
//
//go:noinline
//go:nosplit
func concretePointerBox(v *int) any {
	internalFunc()
	return v
}
