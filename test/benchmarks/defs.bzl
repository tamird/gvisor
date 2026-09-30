"""Defines installed and declared benchmark test targets."""

load("//test/docker:defs.bzl", "docker_test", "owned_docker_test")

# Match the continuous Buildkite benchmark-platforms invocation. KVM stays
# declared even though the current Firecracker workers cannot qualify it.
_CONTINUOUS_RUNTIMES = [
    struct(
        name = platform,
        # Make enables profiling support without enabling debug logging. These
        # arguments follow the private daemon's ordinary --debug default.
        args = ["--platform=" + platform, "--profile", "--debug=false"],
        test_args = [],
        tags = ["requires-kvm"] if platform == "kvm" else [],
    )
    for platform in ["kvm", "systrap"]
] + [struct(name = "runc", args = [], test_args = ["--runtime=runc"], tags = [])]

def benchmark_test(name, tags = [], use_for_pgo = True, continuous = [], cohort = None, owned_args = [], runtime_variants = None, nogo = True, **kwargs):
    """Declares existing benchmark entrypoints and optional continuous cases.

    Args:
      name: Existing installed benchmark target.
      tags: Existing benchmark tags.
      use_for_pgo: Whether the benchmark participates in PGO collection.
      continuous: Case dictionaries with name, cohort, filter and benchtime.
      cohort: Optional image cohort for the existing smoke entrypoint.
      owned_args: Existing smoke entrypoint's arguments.
      runtime_variants: Existing smoke entrypoint's runtime modes.
      nogo: Whether the installed target owns static analysis.
      **kwargs: Existing go_test source, dependency and other attributes.
    """
    tags = tags + [
        "manual",
        "gvisor_benchmark",
    ]
    if use_for_pgo:
        tags = tags + ["gvisor_pgo_benchmark"]

    docker_test(
        name = name,
        cohort = cohort,
        owned_args = owned_args,
        runtime_variants = runtime_variants,
        nogo = nogo,
        tags = tags,
        # Benchmark test binaries are built inside a bazel docker container in
        # OSS but are executed directly on the host. Use static binaries to
        # avoid hitting glibc incompatibility.
        features = ["fully_static_link"],
        **kwargs
    )

    if continuous:
        continuous_kwargs = dict(kwargs)

        # These are separate complete continuous workloads, not the existing
        # one-iteration startup smoke. CI permits 120 minutes across runtimes.
        continuous_kwargs.update(size = "large", timeout = "eternal")
        owned_docker_test(
            name = name + "_continuous",
            memory = "8GB",
            free_disk = "40GB",
            runtime_variants = [
                struct(
                    name = (case["name"] + "_" if case["name"] else "") + runtime.name,
                    cohort = case["cohort"],
                    args = runtime.args,
                    test_args = [
                        "-test.v",
                        "-test.bench=" + case["filter"],
                        "-test.benchtime=" + case["benchtime"],
                    ] + runtime.test_args,
                    tags = runtime.tags,
                )
                for case in continuous
                for runtime in _CONTINUOUS_RUNTIMES
            ],
            tags = tags + ["continuous-benchmark"],
            features = ["fully_static_link"],
            **continuous_kwargs
        )
