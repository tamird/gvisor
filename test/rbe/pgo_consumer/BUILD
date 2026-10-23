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

load("//tools:defs.bzl", "go_reset_target")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# The merger must not depend on the profile it produces. The tool transition
# clears pgoprofile; genrule's tools attribute also selects the execution CPU.
go_reset_target(
    name = "profiletool",
    dep = "//tools/profiletool",
)

# The profile is data: AMD64 releases also analyze ARM64 templates. Keep the
# measured-platform constraint on the release consumer, not this merge.
genrule(
    name = "merged_profile",
    srcs = [
        "ffmpeg.pgo.pprof.pb.gz",
        "iperf.pgo.pprof.pb.gz",
        "nginx.pgo.pprof.pb.gz",
        "redis.pgo.pprof.pb.gz",
        "rubydev.pgo.pprof.pb.gz",
        "sysbench.pgo.pprof.pb.gz",
    ],
    outs = ["go1.27.3-linux-amd64-systrap.pprof.pb.gz"],
    cmd = '$(execpath :profiletool) merge --out="$@" $(SRCS)',
    tags = [
        "manual",
        "qualification-output-group=default",
    ],
    tools = [":profiletool"],
    visibility = ["//visibility:public"],
)

# Retain the public release's default files through the existing recorder.
filegroup(
    name = "release",
    srcs = ["//:release"],
    tags = [
        "manual",
        "qualification-output-group=default",
    ],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)
