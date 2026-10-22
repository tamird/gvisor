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

package state

import (
	"bytes"
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"

	"gvisor.dev/gvisor/pkg/state/wire"
)

func init() {
	registerBinary((*binaryFailure)(nil))
}

var errBinary = errors.New("binary codec failed")

type binaryFailure struct {
	failAppend bool
}

func (b *binaryFailure) AppendBinary(data []byte) ([]byte, error) {
	if b.failAppend {
		return nil, errBinary
	}
	return data, nil
}

func (*binaryFailure) UnmarshalBinary([]byte) error { return errBinary }

func TestBinaryErrors(t *testing.T) {
	var buf bytes.Buffer
	if _, err := Save(t.Context(), &buf, &binaryFailure{failAppend: true}); !errors.Is(err, errBinary) {
		t.Fatalf("Save = %v, want %v", err, errBinary)
	}
	buf.Reset()
	if _, err := Save(t.Context(), &buf, &binaryFailure{}); err != nil {
		t.Fatalf("Save: %v", err)
	}
	if _, err := Load(t.Context(), &buf, &binaryFailure{}); !errors.Is(err, errBinary) {
		t.Fatalf("Load = %v, want %v", err, errBinary)
	}
}

// lazyType counts StateFields calls.
type lazyType struct {
	v int64
}

var lazyTypeFieldsCalls int

func (*lazyType) StateTypeName() string { return "state.lazyType" }

func (*lazyType) StateFields() []string {
	lazyTypeFieldsCalls++
	return []string{"v"}
}

func (l *lazyType) StateSave(s Sink) { s.Save(0, &l.v) }

func (l *lazyType) StateLoad(_ context.Context, s Source) { s.Load(0, &l.v) }

// duplicateFieldType is never registered: race builds would reject it at init.
type duplicateFieldType struct {
	v int64
}

func (*duplicateFieldType) StateTypeName() string { return "state.duplicateFieldType" }

func (*duplicateFieldType) StateFields() []string { return []string{"v", "v"} }

func (d *duplicateFieldType) StateSave(s Sink) { s.Save(0, &d.v) }

func (d *duplicateFieldType) StateLoad(_ context.Context, s Source) { s.Load(0, &d.v) }

// TestRegisterIsLazy checks that Register stays cheap in non-race builds,
// since it runs in the init functions of every binary on every start.
func TestRegisterIsLazy(t *testing.T) {
	if raceEnabled {
		t.Skip("race builds validate types eagerly")
	}
	Register((*lazyType)(nil))
	if lazyTypeFieldsCalls != 0 {
		t.Errorf("Register called StateFields %d times, want 0", lazyTypeFieldsCalls)
	}
	typ := reflect.TypeFor[lazyType]()
	if _, ok := reverseTypeDatabase[typ]; ok {
		t.Errorf("Register populated reverseTypeDatabase")
	}
	if got := globalTypeDatabase["state.lazyType"]; got != typ {
		t.Errorf("globalTypeDatabase[%q] = %v, want %v", "state.lazyType", got, typ)
	}

	var buf bytes.Buffer
	if _, err := Save(t.Context(), &buf, &lazyType{v: 42}); err != nil {
		t.Fatalf("Save: %v", err)
	}
	var got lazyType
	if _, err := Load(t.Context(), &buf, &got); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got.v != 42 {
		t.Errorf("Load: v = %d, want 42", got.v)
	}
}

// TestLazyValidation checks that types skipped by Register are validated on use.
func TestLazyValidation(t *testing.T) {
	if raceEnabled {
		t.Skip("race builds validate types eagerly")
	}
	var buf bytes.Buffer
	_, err := Save(t.Context(), &buf, &duplicateFieldType{})
	if err == nil || !strings.Contains(err.Error(), "duplicate field") {
		t.Fatalf("Save = %v, want duplicate field error", err)
	}
}

func TestArrayPartialStores(t *testing.T) {
	for _, test := range []struct {
		name   string
		values []wire.Object
		target any
		want   any
	}{
		{"signed", []wire.Object{wire.Int(7), wire.Int(128), wire.Int(9)}, &[3]int8{99, 99, 99}, [3]int8{7, -128, 99}},
		{"unsigned", []wire.Object{wire.Uint(7), wire.Uint(256), wire.Uint(9)}, &[3]uint8{99, 99, 99}, [3]uint8{7, 0, 99}},
		{"float", []wire.Object{wire.Float64(7), wire.Float64(1 + 1e-10), wire.Float64(9)}, &[3]float32{99, 99, 99}, [3]float32{7, 1, 99}},
		{"complex", []wire.Object{new(wire.Complex128(7)), new(wire.Complex128(complex(1, 1+1e-10))), new(wire.Complex128(9))}, &[3]complex64{99, 99, 99}, [3]complex64{7, 1 + 1i, 99}},
	} {
		t.Run(test.name, func(t *testing.T) {
			var saved bytes.Buffer
			writer := wire.Writer{Writer: &saved}
			if err := WriteHeader(&writer, 1, true); err != nil {
				t.Fatal(err)
			}
			wire.Save(&writer, wire.Uint(1))
			wire.Save(&writer, &wire.ObjectArray{Contents: test.values})
			if _, err := Load(t.Context(), &saved, test.target); err == nil || !strings.Contains(err.Error(), "truncated") {
				t.Fatalf("Load = %v, want truncation error", err)
			}
			if got, want := reflect.ValueOf(test.target).Elem().Interface(), test.want; !reflect.DeepEqual(got, want) {
				t.Errorf("partial destination = %v, want %v", got, want)
			}
		})
	}
}

func TestArrayDestinationFlags(t *testing.T) {
	private := struct{ values [2]int64 }{values: [2]int64{17, 29}}
	slice := []int64{17, 29}
	for _, test := range []struct {
		name    string
		target  reflect.Value
		wantErr string
	}{
		{"read_only", reflect.ValueOf(&private).Elem().Field(0), "array element of type int64 is not settable"},
		{"unaddressable_array", reflect.ValueOf([2]int64{17, 29}), "array element of type int64 is not settable"},
		{"unaddressable_slice", reflect.ValueOf(slice), ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			// Use the public parser so this exercises its actual typed payload.
			var saved bytes.Buffer
			wire.Save(&wire.Writer{Writer: &saved}, &wire.ObjectArray{Contents: []wire.Object{wire.Int(7), wire.Int(11)}})
			encoded := wire.Load(&wire.Reader{Reader: &saved})
			var ds decodeState
			err := safely(func() { ds.loadValue(nil, reflectedHandle(test.target), encoded) })
			if test.wantErr == "" {
				if err != nil {
					t.Fatal(err)
				}
			} else if err == nil || !strings.Contains(err.Error(), test.wantErr) {
				t.Fatalf("restore error = %v, want %q", err, test.wantErr)
			}
		})
	}
	if got, want := private.values, [2]int64{17, 29}; got != want {
		t.Errorf("read-only destination = %v, want %v", got, want)
	}
	if got, want := slice, []int64{7, 11}; !reflect.DeepEqual(got, want) {
		t.Errorf("slice destination = %v, want %v", got, want)
	}
}

// Preserve supported array-to-slice and interface delegation at the same
// public Load boundary that rejects wrong-shaped empty values below.
func TestLegacyValueDestinations(t *testing.T) {
	var nilInterface any
	var integerInterface any = int64(17)
	for _, test := range []struct {
		name        string
		source      any
		destination any
		want        any
	}{
		{"array_in_slice", &[2]int64{17, 29}, &[]int64{0, 0}, []int64{17, 29}},
		{"interface_in_scalar", &integerInterface, new(int64), int64(17)},
		{"nil_interface_in_scalar", &nilInterface, new(int64(23)), int64(23)},
	} {
		t.Run(test.name, func(t *testing.T) {
			var buf bytes.Buffer
			if _, err := Save(t.Context(), &buf, test.source); err != nil {
				t.Fatal(err)
			}
			if _, err := Load(t.Context(), &buf, test.destination); err != nil {
				t.Fatal(err)
			}
			if got, want := reflect.ValueOf(test.destination).Elem().Interface(), test.want; !reflect.DeepEqual(got, want) {
				t.Errorf("Load = %v, want %v", got, want)
			}
		})
	}
}

func TestNilLeavesDestination(t *testing.T) {
	for _, encoded := range []wire.Object{wire.Nil{}, &wire.Interface{Type: wire.TypeSpecNil{}, Value: wire.Nil{}}} {
		// A non-nil channel is otherwise an unsupported decode destination.
		integer, channel := int64(17), make(chan int)
		for _, destination := range []any{&integer, &channel} {
			original := reflect.ValueOf(destination).Elem().Interface()
			var buf bytes.Buffer
			writer := wire.Writer{Writer: &buf}
			if err := WriteHeader(&writer, 1, true); err != nil {
				t.Fatal(err)
			}
			wire.Save(&writer, wire.Uint(1))
			wire.Save(&writer, encoded)
			if _, err := Load(t.Context(), &buf, destination); err != nil {
				t.Fatal(err)
			}
			if got, want := reflect.ValueOf(destination).Elem().Interface(), original; got != want {
				t.Errorf("zero %T changed destination to %v, want %v", encoded, got, want)
			}
		}
	}
}

// Empty collections and zero references must still match the destination type.
func TestWrongDestinationShape(t *testing.T) {
	emptyStruct := wire.Struct{}
	emptyStruct.Alloc(0)
	for _, test := range []struct {
		name        string
		encoded     wire.Object
		destination any
		wantErr     string
	}{
		{"array_in_string", &wire.ObjectArray{}, new(string), "is *wire.ObjectArray, not *wire.String"},
		{"array_in_map", &wire.ObjectArray{}, new(map[int]int), "is *wire.ObjectArray, not *wire.Map"},
		{"map_in_pointer", &wire.Map{}, new(*int), "is *wire.Map, not *wire.Ref"},
		{"struct_in_integer", &emptyStruct, new(int64), "is *wire.Struct, not wire.Int"},
		{"map_in_slice", &wire.Map{}, new([]int64), "*wire.Map is not wire.Array"},
		{"reference_in_integer", &wire.Ref{}, new(int64), "is *wire.Ref, not wire.Int"},
		{"slice_in_integer", &wire.Slice{}, new(int64), "is *wire.Slice, not wire.Int"},
		{"reference_in_channel", &wire.Ref{}, new(chan int), "cannot restore *wire.Ref into chan int"},
	} {
		t.Run(test.name, func(t *testing.T) {
			var buf bytes.Buffer
			writer := wire.Writer{Writer: &buf}
			if err := WriteHeader(&writer, 1, true); err != nil {
				t.Fatal(err)
			}
			wire.Save(&writer, wire.Uint(1))
			wire.Save(&writer, test.encoded)
			_, err := Load(t.Context(), &buf, test.destination)
			if _, ok := errors.AsType[*ErrState](err); !ok || !strings.Contains(err.Error(), test.wantErr) || !strings.Contains(err.Error(), "error decoding object ID 1") {
				t.Fatalf("Load error = %v, want ErrState with object context and %q", err, test.wantErr)
			}
		})
	}
}
