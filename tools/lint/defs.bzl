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

"""Source checks with declared tools and indexed inputs."""

load("@rules_python//python:defs.bzl", "py_binary", "py_test")
load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")

# Fixers execute in the calling workspace, using binaries for its host.
# buildifier: disable=unused-variable
_fix_binary, _fix_transition = with_cfg(py_binary).set("platforms", [Label("@platforms//host")]).build()

# buildifier: disable=unused-variable
fixer_test, _fixer_test_transition = with_cfg(py_test).set("platforms", [Label("@platforms//host")]).build()

def source_lint(name, group, tool):
    """Declares a source check.

    Args:
        name: Checker name.
        group: Indexed source manifest to check.
        tool: Declared checker executable.
    """
    manifest = "@analysis_sources//:" + group + ".json"
    args = [
        "--check=" + name,
        "--sources=$(rlocationpath %s)" % manifest,
        "--tool=$(rlocationpath %s)" % tool,
    ]
    common = dict(
        srcs = ["runner.py"],
        main = "runner.py",
        deps = ["@rules_python//python/runfiles"],
    )
    py_test(
        name = name,
        size = "medium",
        args = args,
        data = [manifest, "@analysis_sources//:" + group + "_files", tool],
        tags = ["manual"],
        **common
    )
    _fix_binary(
        name = name + "_fix",
        args = args + ["--fix"],
        data = [manifest, tool],
        tags = ["manual"],
        **common
    )
