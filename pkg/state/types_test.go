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

var earlyBinaryError error

func init() {
	// Using a type during an earlier init must not prevent a later init from
	// registering its wire schema. The subsequent saves below must find the
	// newly registered codec.
	var buf bytes.Buffer
	_, earlyBinaryError = Save(context.Background(), &buf, &binaryFailure{})
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
	if earlyBinaryError == nil {
		t.Fatal("saving the unregistered binary type during init succeeded")
	}
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

type lazyRegistered struct {
	value int64
}

var lazyNames, lazyFields int

func (*lazyRegistered) StateTypeName() string {
	lazyNames++
	return "state.lazyRegistered"
}

func (*lazyRegistered) StateFields() []string {
	lazyFields++
	return []string{"value"}
}

func (l *lazyRegistered) StateSave(s Sink) { s.Save(0, &l.value) }

func (l *lazyRegistered) StateLoad(_ context.Context, s Source) {
	s.Load(0, &l.value)
}

func TestLazyRegistration(t *testing.T) {
	t.Cleanup(func() {
		delete(globalTypeDatabase, "state.lazyRegistered")
		delete(reverseTypeDatabase, reflect.TypeFor[lazyRegistered]())
	})
	lazyNames, lazyFields = 0, 0
	// Describing a destination before registration must neither call
	// its metadata on nil wire data nor cache a global fallback.
	var buf bytes.Buffer
	writer := wire.Writer{Writer: &buf}
	if err := WriteHeader(&writer, 1, true); err != nil {
		t.Fatal(err)
	}
	wire.Save(&writer, wire.Uint(1))
	wire.Save(&writer, wire.Nil{})
	value := lazyRegistered{value: 17}
	if _, err := Load(t.Context(), &buf, &value); err != nil {
		t.Fatal(err)
	}
	if got, want := value.value, int64(17); got != want {
		t.Errorf("nil Load value = %d, want %d", got, want)
	}
	if lazyNames != 0 || lazyFields != 0 {
		t.Fatalf("nil Load called metadata: names=%d fields=%d, want zero", lazyNames, lazyFields)
	}
	Register((*lazyRegistered)(nil))
	wantFields := 0
	if raceEnabled {
		wantFields = 1
	}
	if got, want := lazyFields, wantFields; got != want {
		t.Fatalf("registration StateFields calls = %d, want %d", got, want)
	}
	// A new Load must use the registration even if an earlier Load
	// saw this type before registration. Both references share one
	// allocation and exercise the exact registered loader.
	buf.Reset()
	original := [2]*lazyRegistered{&value, &value}
	if _, err := Save(t.Context(), &buf, &original); err != nil {
		t.Fatal(err)
	}
	var restored [2]*lazyRegistered
	if _, err := Load(t.Context(), &buf, &restored); err != nil {
		t.Fatal(err)
	}
	if restored[0] == nil || restored[0] != restored[1] || restored[0] == &value || restored[0].value != value.value {
		t.Fatalf("Load = %v, want two aliases of a new value %d", restored, value.value)
	}
}

// Promoting Type through a value field requires a non-nil outer receiver.
// A Nil body must not call those methods even when allocating a new object.
type promotedMetadata struct{ lazyRegistered }

func TestNilReferencedObject(t *testing.T) {
	var buf bytes.Buffer
	writer := wire.Writer{Writer: &buf}
	if err := WriteHeader(&writer, 2, true); err != nil {
		t.Fatal(err)
	}
	wire.Save(&writer, wire.Uint(1))
	wire.Save(&writer, &wire.Ref{Root: 2})
	wire.Save(&writer, wire.Uint(2))
	wire.Save(&writer, wire.Nil{})
	var restored *promotedMetadata
	if _, err := Load(t.Context(), &buf, &restored); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if restored == nil {
		t.Fatal("Load left the nonzero reference nil")
	}
	if got, want := restored.value, int64(0); got != want {
		t.Errorf("Load value = %d, want %d", got, want)
	}
}

type duplicateFieldType struct{ value int64 }

func (*duplicateFieldType) StateTypeName() string { return "state.duplicateFieldType" }
func (*duplicateFieldType) StateFields() []string { return []string{"value", "value"} }
func (d *duplicateFieldType) StateSave(s Sink)    { s.Save(0, &d.value) }
func (d *duplicateFieldType) StateLoad(_ context.Context, s Source) {
	s.Load(0, &d.value)
}

func TestRegistrationValidation(t *testing.T) {
	t.Cleanup(func() {
		delete(globalTypeDatabase, "state.duplicateFieldType")
		delete(reverseTypeDatabase, reflect.TypeFor[duplicateFieldType]())
	})
	err := safely(func() { Register((*duplicateFieldType)(nil)) })
	if !raceEnabled {
		if err != nil {
			t.Fatalf("registration validated fields eagerly: %v", err)
		}
		var buf bytes.Buffer
		_, err = Save(t.Context(), &buf, &duplicateFieldType{})
	}
	if err == nil || !strings.Contains(err.Error(), "duplicate field") {
		t.Fatalf("validation = %v, want duplicate field error", err)
	}
}

func TestMalformedValueDestinations(t *testing.T) {
	field := wire.FieldName("missing")
	arrayType := &wire.TypeSpecArray{Count: 1, Type: wire.TypeID(1)}
	for _, test := range []struct {
		name        string
		destination any
		encoded     wire.Object
	}{
		{"scalar_kind", new(int64), wire.Bool(true)},
		{"array_kind", new(int64), &wire.Array{}},
		{"field_in_array", new(*int64), &wire.Ref{Root: 2, Type: arrayType, Dots: []wire.Dot{&field}}},
		{"index_at_length", new(*int64), &wire.Ref{Root: 2, Type: arrayType, Dots: []wire.Dot{wire.Index(1)}}},
		{"index_maximum", new(*int64), &wire.Ref{Root: 2, Type: arrayType, Dots: []wire.Dot{^wire.Index(0)}}},
		{"pointer_type", new(*uint64), &wire.Ref{Root: 2, Type: arrayType, Dots: []wire.Dot{wire.Index(0)}}},
		{"slice_length", new([]int64), &wire.Slice{Ref: wire.Ref{Root: 2}, Length: 2, Capacity: 1}},
		{"slice_capacity", new([]int64), &wire.Slice{Ref: wire.Ref{Root: 2}, Capacity: wire.Uint(^uint64(0))}},
		{"slice_storage", new([]int64), &wire.Slice{Ref: wire.Ref{Root: 2}, Capacity: 1 << 61}},
	} {
		t.Run(test.name, func(t *testing.T) {
			var buf bytes.Buffer
			writer := wire.Writer{Writer: &buf}
			if err := WriteHeader(&writer, 1, true); err != nil {
				t.Fatal(err)
			}
			wire.Save(&writer, &wire.Type{Name: "int64"})
			wire.Save(&writer, wire.Uint(1))
			wire.Save(&writer, test.encoded)
			_, err := Load(t.Context(), &buf, test.destination)
			var stateError *ErrState
			if !errors.As(err, &stateError) {
				t.Fatalf("Load = %v, want ErrState", err)
			}
		})
	}
}

func TestPromotedChildPath(t *testing.T) {
	type inner struct{ Value int }
	root := struct{ *inner }{inner: &inner{Value: 17}}
	name := wire.FieldName("Value")
	var buf bytes.Buffer
	writer := wire.Writer{Writer: &buf}
	wire.Save(&writer, &wire.Ref{Root: 1, Dots: []wire.Dot{&name}, Type: wire.TypeID(0)})
	reader := wire.Reader{Reader: &buf}
	child := walkChild(wire.Load(&reader).(*wire.Ref), reflect.ValueOf(&root).Elem())
	if got, want := child.Addr().Pointer(), reflect.ValueOf(&root.Value).Pointer(); got != want {
		t.Fatalf("promoted field address = %#x, want %#x", got, want)
	}
}

// TestLegacyValueDestinations preserves existing destination conversions while
// changing how the decoder retains object storage. The empty-map case is a raw
// wire value; Save ordinarily encodes a map through a reference.
func TestLegacyValueDestinations(t *testing.T) {
	var nilInterface any
	var integerInterface any = int64(17)
	for _, test := range []struct {
		name        string
		source      any
		encoded     wire.Object
		destination any
		want        any
	}{
		{"array_in_slice", &[2]int64{17, 29}, nil, &[]int64{0, 0}, []int64{17, 29}},
		{"empty_array_in_nil_slice", &[0]int64{}, nil, new([]int64), []int64(nil)},
		{"empty_array_in_empty_string", &[0]int64{}, nil, new(string), ""},
		{"interface_in_scalar", &integerInterface, nil, new(int64), int64(17)},
		{"nil_interface_in_scalar", &nilInterface, nil, new(int64(23)), int64(23)},
		{"empty_map_in_slice", nil, &wire.Map{}, &[]int64{23}, []int64{23}},
	} {
		t.Run(test.name, func(t *testing.T) {
			var buf bytes.Buffer
			if test.source != nil {
				if _, err := Save(t.Context(), &buf, test.source); err != nil {
					t.Fatal(err)
				}
			} else {
				writer := wire.Writer{Writer: &buf}
				if err := WriteHeader(&writer, 1, true); err != nil {
					t.Fatal(err)
				}
				wire.Save(&writer, wire.Uint(1))
				wire.Save(&writer, test.encoded)
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

func TestZeroReferencesLeaveDestination(t *testing.T) {
	for _, encoded := range []wire.Object{wire.Nil{}, &wire.Ref{}, &wire.Slice{}, &wire.Interface{Type: wire.TypeSpecNil{}, Value: wire.Nil{}}} {
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
