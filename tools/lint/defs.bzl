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

load("@rules_python//python:defs.bzl", "py_test")

def source_lint(name, group, tool):
    """Declares a source check.

    Args:
        name: Checker name.
        group: Indexed source manifest to check.
        tool: Declared checker executable.
    """
    manifest = "@analysis_sources//:" + group + ".json"
    py_test(
        name = name,
        size = "medium",
        srcs = ["runner.py"],
        main = "runner.py",
        args = [
            "--check=" + name,
            "--sources=$(rlocationpath %s)" % manifest,
            "--tool=$(rlocationpath %s)" % tool,
        ],
        data = [
            manifest,
            "@analysis_sources//:" + group + "_files",
            tool,
        ],
        deps = ["@rules_python//python/runfiles"],
    )
