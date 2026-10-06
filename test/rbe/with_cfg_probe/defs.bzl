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

"""Probe whether with_cfg configures the test action when wrapping a macro."""

load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")

def _wrapped_test(name, **kwargs):
    # gVisor's maintained test declarations are macros, so test that API path.
    sh_test(name = name, **kwargs)

configured_test, _configured_internal = with_cfg(_wrapped_test).set(
    "test_env",
    ["GVISOR_CGROUP_CONFIG_PROBE=configured"],
).build()
