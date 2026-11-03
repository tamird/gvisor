#!/bin/bash
set -euo pipefail
test "$(id -u)" = 0
test "$(uname -m)" = x86_64
printf 'UID %s\nARCH %s\n' "$(id -u)" "$(uname -m)"
awk '{print "UID_MAP",$1,$2,$3}' /proc/self/uid_map
awk '{print "GID_MAP",$1,$2,$3}' /proc/self/gid_map
awk '$1=="CapEff:" {print "CAP_EFF",$2}' /proc/self/status
binary="$TEST_SRCDIR/$TEST_WORKSPACE/$1"
printf 'SHM_ELF %s\n' "$(sha256sum "$binary" | cut -d ' ' -f 1)"
exec "$binary" --gtest_filter=ShmTest.RmidOwnershipPermissionDenied "--gtest_output=xml:$XML_OUTPUT_FILE"
