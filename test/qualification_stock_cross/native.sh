#!/bin/sh
set -eu
binary="$TEST_SRCDIR/$1"
arch=$(uname -m)
printf 'Native worker: %s\n' "$arch"
test "$arch" = aarch64
machine=$(od -An -j18 -N2 -tu2 "$binary")
test "$((machine))" = 183
printf 'ELF machine: %d\n' "$((machine))"
sha256sum "$binary"
exec "$binary" --gtest_output="xml:$XML_OUTPUT_FILE"
