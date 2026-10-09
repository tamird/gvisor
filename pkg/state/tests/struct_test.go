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
	"bytes"
	"math"
	"math/rand"
	"slices"
	"strings"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/tests/shadow"
	"gvisor.dev/gvisor/pkg/state/wire"
)

func TestEmptyStruct(t *testing.T) {
	runTestCases(t, false, "plain", []any{
		unregisteredEmptyStruct{},
		typeOnlyEmptyStruct{},
		savableEmptyStruct{},
	})
	runTestCases(t, false, "pointers", pointersTo([]any{
		unregisteredEmptyStruct{},
		typeOnlyEmptyStruct{},
		savableEmptyStruct{},
	}))
	runTestCases(t, false, "interfaces-pass", interfacesTo([]any{
		// Only registered types can be dispatched via interfaces. All
		// other types should fail, even if it is the empty struct.
		savableEmptyStruct{},
	}))
	runTestCases(t, true, "interfaces-fail", interfacesTo([]any{
		unregisteredEmptyStruct{},
		typeOnlyEmptyStruct{},
	}))
	runTestCases(t, false, "interfacesToPointers-pass", interfacesTo(pointersTo([]any{
		savableEmptyStruct{},
	})))
	runTestCases(t, true, "interfacesToPointers-fail", interfacesTo(pointersTo([]any{
		unregisteredEmptyStruct{},
		typeOnlyEmptyStruct{},
	})))

	// Ensuring empty struct aliasing works.
	es := emptyStructPointer{new(struct{})}
	runTestCases(t, false, "empty-struct-pointers", []any{
		emptyStructPointer{},
		es,
		[]emptyStructPointer{es, es}, // Same pointer.
	})
}

func TestEmbeddedPointers(t *testing.T) {
	// Give each int64 a random value to prevent Go from using
	// runtime.staticuint64s, which confounds tests for struct duplication.
	magic := func() int64 {
		for {
			n := rand.Int63()
			if n < 0 || n > 255 {
				return n
			}
		}
	}

	ofs := outerSame{inner{magic()}}
	of1 := outerFieldFirst{inner{magic()}, magic()}
	of2 := outerFieldSecond{magic(), inner{magic()}}
	oa := outerArray{[2]inner{{magic()}, {magic()}}}
	osl := outerSlice{oa.inner[:]}
	ofv := outerFieldValue{innerFieldValue{magic()}}

	runTestCases(t, false, "embedded-pointers", []any{
		system{&ofs, &ofs.inner},
		system{&ofs.inner, &ofs},
		system{&of1, &of1.inner},
		system{&of1.inner, &of1},
		system{&of2, &of2.inner},
		system{&of2.inner, &of2},
		system{&oa, &oa.inner[0]},
		system{&oa, &oa.inner[1]},
		system{&oa.inner[0], &oa},
		system{&oa.inner[1], &oa},
		system3{&oa, &oa.inner[0], &oa.inner[1]},
		system3{&oa, &oa.inner[1], &oa.inner[0]},
		system3{&oa.inner[0], &oa, &oa.inner[1]},
		system3{&oa.inner[1], &oa, &oa.inner[0]},
		system3{&oa.inner[0], &oa.inner[1], &oa},
		system3{&oa.inner[1], &oa.inner[0], &oa},
		system{&oa, &osl},
		system{&osl, &oa},
		system{&ofv, &ofv.inner},
		system{&ofv.inner, &ofv},
	})
}

func TestMultiNameFields(t *testing.T) {
	runTestCases(t, false, "multi-name-field", []any{
		multiName{b: "foo", c: "bar", x: 10, y: 20, z: -30},
	})
}

// typedFieldsWire describes the existing wire contract independently of the
// generated field access path. It can also supply a different field order.
func typedFieldsWire(t *testing.T, fields []string, values []wire.Object, extraTypes ...*wire.Type) []byte {
	t.Helper()
	var buf bytes.Buffer
	w := wire.Writer{Writer: &buf}
	if err := state.WriteHeader(&w, 1, true); err != nil {
		t.Fatalf("WriteHeader: %v", err)
	}
	wire.Save(&w, &wire.Type{Name: (*typedFields)(nil).StateTypeName(), Fields: fields})
	for _, typ := range extraTypes {
		wire.Save(&w, typ)
	}
	wire.Save(&w, wire.Uint(1))
	object := &wire.Struct{TypeID: 1}
	object.Alloc(len(values))
	for i, value := range values {
		*object.Field(i) = value
	}
	wire.Save(&w, object)
	return buf.Bytes()
}

func TestTypedFieldsWire(t *testing.T) {
	fields := []string{"signed", "unsigned", "flag", "text", "f32", "f64", "c64", "c128", "zero", "self"}
	text := wire.String("state")
	c64 := wire.Complex64(2 - 3i)
	c128 := wire.Complex128(4 - 5i)
	values := []wire.Object{wire.Int(-7), wire.Uint(9), wire.Bool(true), &text,
		wire.Float32(1.25), wire.Float64(2.5), &c64, &c128, wire.Nil{}, &wire.Ref{Root: 1}}
	for _, test := range []struct {
		name   string
		cycle  bool
		value  typedFields
		values []wire.Object
	}{
		{"zero", false, typedFields{}, []wire.Object{wire.Nil{}, wire.Nil{}, wire.Nil{}, wire.Nil{}, wire.Nil{}, wire.Nil{}, wire.Nil{}, wire.Nil{}, wire.Nil{}, wire.Nil{}}},
		{"values-and-cycle", true, typedFields{signed: -7, unsigned: 9, flag: true, text: "state", f32: 1.25, f64: 2.5, c64: 2 - 3i, c128: 4 - 5i}, values},
	} {
		t.Run(test.name, func(t *testing.T) {
			original := test.value
			if test.cycle {
				original.self = &original
			}
			var encoded bytes.Buffer
			if _, err := state.Save(t.Context(), &encoded, &original); err != nil {
				t.Fatalf("Save: %v", err)
			}
			if got, want := encoded.Bytes(), typedFieldsWire(t, fields, test.values); !bytes.Equal(got, want) {
				t.Errorf("wire bytes = %x, want %x", got, want)
			}

			// A checkpoint's field order need not match the local declaration.
			// Load must reconcile by name before taking the typed field path.
			reorderedFields, reorderedValues := slices.Clone(fields), slices.Clone(test.values)
			slices.Reverse(reorderedFields)
			slices.Reverse(reorderedValues)
			var loaded typedFields
			if _, err := state.Load(t.Context(), bytes.NewReader(typedFieldsWire(t, reorderedFields, reorderedValues)), &loaded); err != nil {
				t.Fatalf("Load reordered fields: %v", err)
			}
			expected := original
			if original.self != nil {
				expected.self = &loaded
			}
			if got, want := loaded, expected; got != want {
				t.Errorf("loaded fields = %+v, want %+v", got, want)
			}
		})
	}

	wideComplex := wire.Complex128(complex(math.Pi, -math.Pi))
	for _, test := range []struct {
		field string
		value wire.Object
	}{
		{"signed", wire.Int(math.MaxInt16 + 1)},
		{"unsigned", wire.Uint(math.MaxUint32 + 1)},
		{"f32", wire.Float64(math.Pi)},
		{"c64", &wideComplex},
	} {
		t.Run("truncated-"+test.field, func(t *testing.T) {
			encodedValues := slices.Clone(values)
			encodedValues[slices.Index(fields, test.field)] = test.value
			var loaded typedFields
			_, err := state.Load(t.Context(), bytes.NewReader(typedFieldsWire(t, fields, encodedValues)), &loaded)
			if err == nil || !strings.Contains(err.Error(), "truncated") {
				t.Errorf("Load narrowing %s = %v, want a truncation error", test.field, err)
			}
		})
	}

	// Interface wire values are decoded through their declared dynamic type.
	// Matching scalar representations do not make distinct Go types assignable.
	for _, test := range []struct {
		name      string
		typeName  string
		wantError bool
	}{
		{"builtin-interface", "int16", true},
		{"named-interface", (*typedSigned)(nil).StateTypeName(), false},
	} {
		t.Run(test.name, func(t *testing.T) {
			encodedValues := slices.Clone(values)
			encodedValues[0] = &wire.Interface{Type: wire.TypeID(2), Value: wire.Int(-7)}
			var loaded typedFields
			_, err := state.Load(t.Context(), bytes.NewReader(typedFieldsWire(t, fields, encodedValues, &wire.Type{Name: test.typeName})), &loaded)
			if got, want := err != nil, test.wantError; got != want {
				t.Fatalf("Load error = %v, want error %t", err, want)
			}
			if !test.wantError {
				if got, want := loaded.signed, typedSigned(-7); got != want {
					t.Errorf("loaded signed = %v, want %v", got, want)
				}
			}
		})
	}

}

func TestGeneratedFieldTypes(t *testing.T) {
	runTestCases(t, false, "shadowed-and-imported", []any{
		shadow.Value{Number: "not an integer", Duration: 3 * time.Second},
		shadow.A_B{C: "first field"},
		shadow.A{B_C: "second field"},
	})
}
