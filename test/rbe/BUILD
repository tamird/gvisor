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

load("//tools:defs.bzl", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_test(
    name = "ollama_client_test",
    srcs = ["ollama_client_test.go"],
    tags = ["manual"],
    deps = ["//pkg/test/dockerutil"],
)

go_test(
    name = "profile_helper_test",
    srcs = ["profile_helper_test.go"],
    tags = ["manual"],
    deps = [
        "//pkg/test/dockerutil",
        "@com_github_docker_docker//api/types/mount:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)
