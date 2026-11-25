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

package sched

import (
	"testing"
)

func TestNumCPUs(t *testing.T) {
	for i := range uint(1024) {
		c := NewCPUSet(i)
		for j := range i {
			c.Set(j)
		}
		n := c.NumCPUs()
		if n != i {
			t.Errorf("got wrong number of cpus %d, want %d", n, i)
		}
	}
}

func TestClearAbove(t *testing.T) {
	const n = 1024
	c := NewFullCPUSet(n)
	for i := range uint(n) {
		cpu := n - i
		c.ClearAbove(cpu)
		if got := c.NumCPUs(); got != cpu {
			t.Errorf("iteration %d: got %d cpus, wanted %d", i, got, cpu)
		}
	}
}
