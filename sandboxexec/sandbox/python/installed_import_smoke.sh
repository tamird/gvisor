#!/bin/bash

# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail
installer=$(realpath "$1")
importer=$(realpath "$2")
dist=$(realpath "$3")
set -- "$dist"/*.whl
[[ $# == 1 && -f $1 ]]
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH
"$installer" --prefix "$TEST_TMPDIR/install" --no-compile-bytecode --validate-record all "$1"
site="$TEST_TMPDIR/install/lib/python3.11/site-packages"
[[ -f "$site/gvisor/__init__.py" && -f "$site/gvisor/sandbox.py" ]]
cd "$TEST_TMPDIR"
log="$TEST_TMPDIR/import.log"
if ! PYTHONPATH="$site" "$importer" >"$log" 2>&1; then
  cat "$log"
  exit 1
fi
grep -F "$site/gvisor/__init__.py" "$log"
grep -F "$site/gvisor/sandbox.py" "$log"
echo 'Imported the wheel installed by the standard installer into the isolated prefix.'
