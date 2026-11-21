#!/bin/bash

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

# Runs an owned Docker worker on a native Linux Bazel test host.
# Requires Docker tools, sudo, unshare, mount and Python 3 on the host.
set -euo pipefail

: "${1:?test executable is required}" "${XML_OUTPUT_FILE:?}"

# The rules_go report writer overwrites this file without replacing it.
# Create it as the caller so elevation does not change its ownership.
python3 - "$XML_OUTPUT_FILE" <<'PYXML'
import os
import sys

_, output = sys.argv
fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
os.close(fd)
PYXML

exec sudo -E unshare --mount --net --propagation private sh -eu -c '
# Keep dockerd from attaching to the host containerd across mount namespaces.
mount -t tmpfs -o mode=0755 tmpfs /run/containerd
exec "$@"
' -- "$@"
