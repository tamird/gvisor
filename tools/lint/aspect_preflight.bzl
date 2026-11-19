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

"""Host-tool selection for the Aspect formatter integration preflight."""

load("@rules_multirun//:defs.bzl", "command")
load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")

# Fixers run in the caller's workspace, so their tools must run on its host.
# buildifier: disable=unused-variable
host_command, _host_command_transition = with_cfg(command).set("platforms", [Label("@platforms//host")]).build()
