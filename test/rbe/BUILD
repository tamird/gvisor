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

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//visibility:public"],
    licenses = ["notice"],
)

# Validation-only worker routing; the source feature is provider-independent.
_EXEC_PROPERTIES = {
    "OSFamily": "linux",
    "container-image": "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
    "workload-isolation-type": "oci",
}

platform(
    name = "native_amd64",
    exec_properties = dict(
        _EXEC_PROPERTIES,
        Arch = "amd64",
    ),
    parents = ["@io_bazel_rules_go//go/toolchain:linux_amd64_cgo"],
)

platform(
    name = "native_arm64",
    exec_properties = dict(
        _EXEC_PROPERTIES,
        Arch = "arm64",
    ),
    parents = ["@io_bazel_rules_go//go/toolchain:linux_arm64_cgo"],
)
