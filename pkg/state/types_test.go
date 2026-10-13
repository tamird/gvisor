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
	child := walkChild([]wire.Dot{&name}, reflect.ValueOf(&root).Elem())
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
