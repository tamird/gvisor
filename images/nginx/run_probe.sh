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

if [ "$(uname -m)" = "$NGX_TARGET_MACHINE" ]; then
    exec "$@"
fi

# Match target_emulator's ARM CPU selection, used by existing Go generators.
case "$NGX_TARGET_MACHINE" in
    aarch64) exec "$NGINX_EMULATOR" -cpu neoverse-n1 "$@" ;;
    x86_64) exec "$NGINX_EMULATOR" "$@" ;;
    *) echo "Unsupported Nginx probe architecture: $NGX_TARGET_MACHINE" >&2; exit 1 ;;
esac
