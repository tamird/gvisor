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
if (( $# != 1 )); then
  echo "usage: $0 <output-private-key>" >&2
  exit 1
fi

# This key is only for testing. Isolate generation from the caller's keyring.
key_dir=$(mktemp -d)
cleanup() {
  local status=$?
  gpgconf --homedir "$key_dir" --kill all || status=1
  rm -rf "$key_dir" || status=1
  exit "$status"
}
trap cleanup EXIT
options=(--homedir "$key_dir" --batch --no-tty)
if gpg --pinentry-mode loopback --version >/dev/null 2>&1; then
  options+=(--pinentry-mode loopback)
fi
cat > "$key_dir/config" <<'KEY'
Key-Type: DSA
Key-Length: 1024
Name-Real: Test
Name-Email: test@example.com
Expire-Date: 0
%commit
KEY
gpg "${options[@]}" --passphrase '' --gen-key "$key_dir/config"
gpg "${options[@]}" --export-secret-keys > "$1"
