#!/bin/bash
set -euo pipefail
exec "$TEST_SRCDIR/$TEST_WORKSPACE/test/rbe/tcg/vfio_pci_probe_bin" --gtest_filter=VfioPCIDeviceTest.Readiness
