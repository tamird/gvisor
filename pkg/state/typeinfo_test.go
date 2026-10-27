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
	"reflect"
	"testing"
)

type directTestStruct struct {
	p *int
}

func TestIsDirect(t *testing.T) {
	for _, typ := range []reflect.Type{
		reflect.TypeFor[int](),
		reflect.TypeFor[string](),
		reflect.TypeFor[*int](),
		reflect.TypeFor[map[int]int](),
		reflect.TypeFor[chan int](),
		reflect.TypeFor[func()](),
		reflect.TypeFor[[]*int](),
		reflect.TypeFor[[0]*int](),
		reflect.TypeFor[[1]*int](),
		reflect.TypeFor[[2]*int](),
		reflect.TypeFor[[1][1]*int](),
		reflect.TypeFor[[1]int](),
		reflect.TypeFor[struct{}](),
		reflect.TypeFor[directTestStruct](),
		reflect.TypeFor[struct{ a [1]*int }](),
		reflect.TypeFor[[1]directTestStruct](),
		reflect.TypeFor[struct {
			p *int
			z struct{}
		}](),
		reflect.TypeFor[struct {
			z struct{}
			p *int
		}](),
		reflect.TypeFor[struct{ i int }](),
	} {
		// Values stored indirectly are pointed to by a non-nil data word,
		// even for zero values. The data word holds zero values of direct
		// types.
		v := reflect.Zero(typ).Interface()
		want := efaceOf(&v).data == nil
		if got := isDirect(typ); got != want {
			t.Errorf("isDirect(%v) = %t, want %t", typ, got, want)
		}
	}
}
