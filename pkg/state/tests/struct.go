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

package tests

import (
	"context"

	"gvisor.dev/gvisor/pkg/state"
)

type unregisteredEmptyStruct struct{}

// typeOnlyEmptyStruct just implements the state.Type interface.
type typeOnlyEmptyStruct struct{}

func (*typeOnlyEmptyStruct) StateTypeName() string { return "registeredEmptyStruct" }

func (*typeOnlyEmptyStruct) StateFields() []string { return nil }

// +stateify savable
type savableEmptyStruct struct{}

// +stateify savable
type emptyStructPointer struct {
	nothing *struct{}
}

// +stateify savable
type outerSame struct {
	inner inner
}

// +stateify savable
type outerFieldFirst struct {
	inner inner
	v     int64
}

// +stateify savable
type outerFieldSecond struct {
	v     int64
	inner inner
}

// +stateify savable
type outerArray struct {
	inner [2]inner
}

// +stateify savable
type outerSlice struct {
	inner []inner
}

// +stateify savable
type inner struct {
	v int64
}

// +stateify savable
type outerFieldValue struct {
	inner innerFieldValue
}

// +stateify savable
type innerFieldValue struct {
	v int64 `state:".(*savedFieldValue)"`
}

// +stateify savable
type savedFieldValue struct {
	v int64
}

func (ifv *innerFieldValue) saveV() *savedFieldValue {
	return &savedFieldValue{ifv.v}
}

func (ifv *innerFieldValue) loadV(_ context.Context, sfv *savedFieldValue) {
	ifv.v = sfv.v
}

// +stateify savable
type system struct {
	v1 any
	v2 any
}

// +stateify savable
type system3 struct {
	v1 any
	v2 any
	v3 any
}

// +stateify savable
type multiName struct {
	_, b, c string
	x, y    int64
	z       int32
}

// These defined types check that generated scalar operations preserve the
// underlying representation without requiring an exact builtin type assertion.
// +stateify type
type typedSigned int16
type typedUnsigned uint32
type typedBool bool
type typedString string
type typedFloat float32
type typedComplex complex64
type typedPointer *typedFields

// +stateify savable
type typedFields struct {
	signed   typedSigned
	unsigned typedUnsigned
	flag     typedBool
	text     typedString
	f32      typedFloat
	f64      float64
	c64      typedComplex
	c128     complex128
	zero     typedSigned
	self     typedPointer
}

// +stateify savable
type framedGraph struct {
	value int64
	name  string
	next  *framedGraph
	wait  *framedGraph `state:"wait"`
	loads int          `state:"nosave"`
}

func (g *framedGraph) afterLoad(context.Context) {
	if g.wait != nil && g.wait.loads != 1 {
		panic("dependency hook has not completed")
	}
	g.loads++
}

// +stateify savable
type framedMutator struct {
	target *framedGraph
}

func (m *framedMutator) beforeSave() {
	m.target.value = 99
}

// +stateify savable
type framedHookValue struct {
	value int64 `state:".(int64)"`
	calls int   `state:"nosave"`
}

func (v *framedHookValue) saveValue() int64 {
	v.calls++
	return v.value
}

func (v *framedHookValue) loadValue(_ context.Context, x int64) {
	v.value = x
}

// +stateify savable
type framedHookParent struct {
	child framedHookValue
}

// +stateify savable
type framedPair struct {
	first  int16
	second int16
}

// framedCustom deliberately saves a wider representation than its Go field.
// The generic helper must fall back before treating that pointer as an int16.
type framedCustom struct {
	value    int16
	observed int64
}

func (*framedCustom) StateTypeName() string { return "gvisor.dev/gvisor/pkg/state/tests.framedCustom" }
func (*framedCustom) StateFields() []string { return []string{"value"} }
func (c *framedCustom) StateSave(s state.Sink) {
	// Preserve a high bit that cannot be represented by the declared int16.
	wide := int64(c.value) + 65536
	state.SaveField(s, 0, &wide)
}
func (c *framedCustom) StateLoad(_ context.Context, s state.Source) {
	var wide int64
	state.LoadField(s, 0, &wide, false)
	c.observed = wide
	c.value = int16(wide)
}
func init() { state.Register((*framedCustom)(nil)) }
