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

"""Generate the governance artifacts from their declared inputs."""

load("@bazel_lib//lib:run_binary.bzl", "run_binary")
load("@bazel_lib//lib:write_source_files.bzl", "write_source_file", "write_source_files")
load("//tools/bazeldefs:presubmit_tests.bzl", "arm64_presubmit_executable")

def governance_source_files(name, files):
    """Declares the workspace updater and checks its generated files on both CPUs."""
    updates = []
    tests = []
    for index, (out_file, in_file) in enumerate(files.items()):
        update = "%s_%d" % (name, index)
        test = write_source_file(
            name = update,
            in_file = in_file,
            out_file = out_file,
            suggested_update_target = name,
        )
        updates.append(update)
        tests.append(test)

        # Run bazel-lib's generated test with its original check, message
        # and runfiles, all configured for the ARM64 target.
        arm64_presubmit_executable(test, size = "small")
    write_source_files(name = name, additional_update_targets = updates)
    native.test_suite(name = name + "_tests", tests = tests)

def governance_files(name):
    """Generate CODEOWNERS and MAINTAINERS.md under name."""
    for format in ["CODEOWNERS", "MAINTAINERS.md"]:
        run_binary(
            name = name + "_" + format,
            srcs = [
                "@governance_areas//:areas.yaml",
                "maintainers.yaml",
            ],
            outs = [name + "/" + format],
            args = [
                "-input=$(execpath maintainers.yaml)",
                "-areas=$(execpath @governance_areas//:areas.yaml)",
                "-format=" + format,
                "-output=$@",
            ],
            tool = "//governance/tools/maintainers:maintainers_gen",
            visibility = ["//:__pkg__"],
        )
