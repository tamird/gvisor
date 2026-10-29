load("@rules_python//python:defs.bzl", "py_runtime", "py_runtime_pair")

package(default_visibility = ["//visibility:public"])

py_runtime(
    name = "python",
    files = glob(["lib/python3.11/**/*.py"], exclude = ["lib/python3.11/test/**"]),
    interpreter = "bin/python3.11",
    python_version = "PY3",
)

py_runtime_pair(name = "runtime", py3_runtime = ":python")

toolchain(
    name = "static_python",
    exec_compatible_with = ["@platforms//cpu:x86_64", "@platforms//os:linux"],
    target_compatible_with = ["@platforms//cpu:x86_64", "@platforms//os:linux"],
    toolchain = ":runtime",
    toolchain_type = "@bazel_tools//tools/python:toolchain_type",
)

platform(
    name = "scratch",
    constraint_values = ["@platforms//cpu:x86_64", "@platforms//os:linux"],
    exec_properties = {
        "Arch": "amd64",
        "OSFamily": "linux",
        "container-image": "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef",
        "workload-isolation-type": "oci",
    },
)

platform(
    name = "ubuntu",
    constraint_values = ["@platforms//cpu:x86_64", "@platforms//os:linux"],
    exec_properties = {
        "Arch": "amd64",
        "OSFamily": "linux",
        "container-image": "docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
        "workload-isolation-type": "oci",
    },
)
