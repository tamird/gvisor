"""Defines a rule for benchmark test targets."""

load("//test/docker:defs.bzl", "docker_test")

def benchmark_test(name, tags = [], use_for_pgo = True, **kwargs):
    tags = tags + [
        "manual",
        "gvisor_benchmark",
    ]
    if use_for_pgo:
        tags = tags + ["gvisor_pgo_benchmark"]

    docker_test(
        name = name,
        tags = tags,
        # Benchmark test binaries are built inside a bazel docker container in
        # OSS but are executed directly on the host. Use static binaries to
        # avoid hitting glibc incompatibility.
        features = ["fully_static_link"],
        **kwargs
    )
