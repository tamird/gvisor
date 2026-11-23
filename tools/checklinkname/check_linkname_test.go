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

// Package checklinkname ensures that linkname declarations match their source.
package checklinkname

import (
	"go/token"
	"go/types"
	"testing"
)

func TestCompositeSignatures(t *testing.T) {
	for _, test := range []struct {
		name string
		a    string
		b    string
	}{
		{
			name: "struct",
			a:    "struct{A int; B string}",
			b:    "struct{A int; B bool}",
		},
		{
			name: "interface",
			a:    "interface{A(int); B(string)}",
			b:    "interface{A(int); B(bool)}",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			var signatures [2]string
			for i, expr := range []string{test.a, test.b} {
				tv, err := types.Eval(token.NewFileSet(), nil, token.NoPos, expr)
				if err != nil {
					t.Fatal(err)
				}
				signatures[i], _ = simplifyType(tv.Type, 4)
			}
			if signatures[0] == signatures[1] {
				t.Errorf("%s and %s have the same signature %q", test.a, test.b, signatures[0])
			}
		})
	}
}
