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

set -efu

# target_emulator selects the declared QEMU for the execution CPU. Its
# same-CPU alias deliberately fails, so execute matching targets directly.
if [ "$(uname -m)" = "${HTTPD_TARGET_ARCH:?}" ]; then
    exec "$@"
fi

# Split the declared emulator command into its executable and fixed options.
# foreign_cc has already made the declared executable path absolute.
set -- ${HTTPD_EMULATOR:?} "$@"
emulator=$1
shift
exec "$emulator" "$@"
