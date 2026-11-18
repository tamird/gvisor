// Copyright 2026 The gVisor Authors.
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

package codegen

import (
	"context"
	"fmt"
	"os"
	"regexp"
	"strings"

	"gvisor.dev/gvisor/tools/checkescape"
)

// Calls reads only the requested production functions with the configured SDK.
func Calls(ctx context.Context, archive, goTool string, symbols []string) (map[string][]checkescape.ObjectCall, error) {
	file, err := os.Open(archive)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	patterns := make([]string, 0, len(symbols))
	for _, symbol := range symbols {
		patterns = append(patterns, regexp.QuoteMeta(symbol))
	}
	if len(patterns) == 0 {
		return nil, fmt.Errorf("no production functions requested")
	}
	return checkescape.ObjectCalls(ctx, file, goTool, "^("+strings.Join(patterns, "|")+")$")
}

// WrapperCall is a residual call that should have been inlined.
type WrapperCall struct {
	Caller string
	Target string
}

func (call *WrapperCall) Error() string {
	return fmt.Sprintf("%s retains atomic wrapper call %s", call.Caller, call.Target)
}

// ValidateCalls rejects residual wrappers and unresolved calls in each owner.
// This is a separate check from the positive DWARF inlining requirement.
func ValidateCalls(calls map[string][]checkescape.ObjectCall, symbols []string) error {
	for _, symbol := range symbols {
		entries, ok := calls[symbol]
		if !ok {
			return fmt.Errorf("production TEXT symbol %s missing", symbol)
		}
		for _, call := range entries {
			if !call.Resolved {
				return fmt.Errorf("unresolved call in %s: %s", symbol, call.Target)
			}
			if strings.Contains(call.Target, "AtomicPtr") || strings.Contains(call.Target, "sync/atomic.(*Pointer[") {
				return &WrapperCall{Caller: symbol, Target: call.Target}
			}
		}
	}
	return nil
}
