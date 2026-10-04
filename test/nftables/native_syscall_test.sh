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

binary=$(realpath "$1")
shift
# Match Make's nftables-syscall-runc-tests: native root with network privileges.
exec docker run --rm --runtime=runc --privileged --user=0:0 \
  --volume="$binary:/netfilter_test:ro" gvisor.dev/images/nftables \
  /netfilter_test "$@"
