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

set -euo pipefail

if [[ "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}" != 3.2 ]]; then
  echo "This host compatibility test requires Bash 3.2, got $BASH_VERSION" >&2
  exit 1
fi
printf 'Host interpreter: %s\n' "$BASH_VERSION"
formatter="$TEST_SRCDIR/$1"
export BUILD_WORKSPACE_DIRECTORY="$TEST_TMPDIR/work space"
export BUILD_WORKING_DIRECTORY="$BUILD_WORKSPACE_DIRECTORY"
mkdir -p "$BUILD_WORKSPACE_DIRECTORY/space dir"
cd "$BUILD_WORKSPACE_DIRECTORY"
touch 'space dir/file.go' 'literal[ab].go' 'literala.go'
printf '%s\n' 'space dir/file.go' 'literal[ab].go' > sources.txt

export SEEN="$TEST_TMPDIR/seen"
tool="$TEST_TMPDIR/record-args"
cat > "$tool" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" >> "$SEEN"
exit "${TOOL_STATUS:-0}"
EOF
chmod +x "$tool"
export tool lang=Go mode=fix flags='' disable_git_attribute_checks=true

/bin/bash "$formatter" "@$BUILD_WORKSPACE_DIRECTORY/sources.txt"
cmp sources.txt "$SEEN"

: > "$SEEN"
printf '%s\n%s' 'space dir/file.go' 'literal[ab].go' > unterminated.txt
/bin/bash "$formatter" "@$BUILD_WORKSPACE_DIRECTORY/unterminated.txt"
cmp sources.txt "$SEEN"

: > "$SEEN"
printf '\n%s\n' 'literal[ab].go' > blank.txt
if /bin/bash "$formatter" "@$BUILD_WORKSPACE_DIRECTORY/blank.txt" > "$TEST_TMPDIR/blank.log" 2>&1; then
  echo 'A blank record hid a later valid input' >&2
  exit 1
fi
test ! -s "$SEEN"

: > "$SEEN"
if /bin/bash "$formatter" missing.go > "$TEST_TMPDIR/missing.log" 2>&1; then
  echo 'A missing explicit source was accepted' >&2
  exit 1
fi
test ! -s "$SEEN"

touch empty.txt
if /bin/bash "$formatter" "@$BUILD_WORKSPACE_DIRECTORY/empty.txt" > "$TEST_TMPDIR/empty.log" 2>&1; then
  echo 'An empty parameter file was accepted' >&2
  exit 1
fi

if TOOL_STATUS=7 /bin/bash "$formatter" "@$BUILD_WORKSPACE_DIRECTORY/sources.txt" > "$TEST_TMPDIR/tool.log" 2>&1; then
  echo 'A formatter failure was accepted' >&2
  exit 1
fi
