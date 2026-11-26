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

"""Original Go module inputs for the offline legacy coverage comparison."""

def _module_proxy_impl(ctx):
    sdk = ctx.path(ctx.attr.sdk_root).dirname
    go = sdk.get_child("bin/go")
    ctx.watch(go)
    manifests = {"go.mod": ctx.read(ctx.attr.go_mod), "go.sum": ctx.read(ctx.attr.go_sum)}
    for name, contents in manifests.items():
        ctx.file("work/" + name, contents)
    env = {
        "GOENV": "off",
        "GOFLAGS": "",
        "GO111MODULE": "on",
        "GOPRIVATE": "",
        "GONOPROXY": "",
        "GONOSUMDB": "",
        "GOTOOLCHAIN": "local",
        "GOWORK": "off",
        "GOROOT": str(sdk),
        "GOPATH": str(ctx.path("gopath")),
        "GOCACHE": str(ctx.path("build-cache")),
        "GOMODCACHE": str(ctx.path("modules")),
        "GOPROXY": "https://proxy.golang.org",
        "GOSUMDB": "sum.golang.org",
        "GOVCS": "*:off",
    }
    result = ctx.execute([go, "version"], environment = env)
    if result.return_code or not result.stdout.startswith("go version go1.27.3 "):
        fail("Expected pinned Go 1.27.3: " + result.stdout + result.stderr)
    for command in ["download", "verify"]:
        result = ctx.execute([go, "mod", command], environment = env, working_directory = "work", timeout = 600)
        if result.return_code:
            fail("go mod " + command + " failed: " + result.stdout + result.stderr)
    for name, contents in manifests.items():
        if ctx.read("work/" + name) != contents:
            fail("go mod download changed pinned " + name)
    ctx.file("modules/cache/download/ROOT", "")
    ctx.file("BUILD.bazel", """exports_files(["modules/cache/download/ROOT"])
filegroup(
    name = "proxy",
    srcs = glob(["modules/cache/download/**"], exclude = ["**/*.lock", "**/sumdb/**"]),
    visibility = ["//visibility:public"],
)
""")

coverage_legacy_module_proxy = repository_rule(
    implementation = _module_proxy_impl,
    attrs = {
        "go_mod": attr.label(mandatory = True, allow_single_file = True),
        "go_sum": attr.label(mandatory = True, allow_single_file = True),
        "sdk_root": attr.label(mandatory = True, allow_single_file = True),
    },
)
