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

//go:build !false
// +build !false

package nvproxy_driver_parity_test

import (
	"context"
	"testing"

	"gvisor.dev/gvisor/pkg/sentry/devices/nvproxy"
	"gvisor.dev/gvisor/pkg/sentry/devices/nvproxy/nvconf"
	"gvisor.dev/gvisor/tools/gpu/drivers"
)

// TestDriverChecksums tests that the checksums of all drivers are correct.
func TestDriverChecksums(t *testing.T) {
	ctx := context.Background()
	nvproxy.Init()
	nvproxy.ForEachSupportDriver(func(version nvconf.DriverVersion, checksums nvproxy.Checksums) {
		t.Run(version.String(), func(t *testing.T) {
			t.Parallel()
			if err := drivers.ValidateChecksum(ctx, version.String(), checksums); err != nil {
				t.Errorf("checksum mismatch for driver %q: %v", version.String(), err)
			}
		})
	})
}
