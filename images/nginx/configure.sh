#!/bin/sh
# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -eu

export NGX_TARGET_MACHINE="$1"
export NGINX_EMULATOR="$2"
export NGX_TEST_RUNNER="$3"
configure="$4"
shift 4

# The framework's temporary installation prefix is not a runtime path. Drop
# only that generated argument; the declared /etc/nginx option follows it.
if [ "$1" != "--prefix=$BUILD_TMPDIR/$INSTALL_PREFIX" ]; then
    echo "Expected rules_foreign_cc's temporary --prefix argument" >&2
    exit 1
fi
shift

# Keep toolchain paths in the compiler environment, not nginx -V metadata.
export CFLAGS="${CFLAGS:-} ${CPPFLAGS:-}"

# Nginx's first compiler probe precedes auto/cc/conf's flag initialization.
# Give that probe the same target compiler and linker flags as later probes.
export CC_TEST_FLAGS="$CFLAGS"
export NGX_TEST_LD_OPT="${LDFLAGS:-}"
exec "$configure" \
    --crossbuild="Linux::$NGX_TARGET_MACHINE" \
    "$@"
