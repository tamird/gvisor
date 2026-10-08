"""Moby's public CI deadline on its original Go test declaration."""

load("@io_bazel_rules_go//go:def.bzl", "GoArchive", "GoLibrary", _go_test = "go_test")
load("@with_cfg.bzl//:with_cfg.bzl", "frontend_test", "with_cfg")
load("//test/docker:defs.bzl", "owned_docker_test")
load("//tools:defs.bzl", "go_test", "local_test_tags")

# Make's moby-tests gives each of the four shards 360 seconds. Configure the
# outer TestRunner directly: reading Bazel's native test_timeout map as a
# transition input is unsupported, as with runtime_test_timeout.
_moby_timeout = transition(
    implementation = lambda _settings, _attr: {"//command_line_option:test_timeout": "360"},
    inputs = [],
    outputs = ["//command_line_option:test_timeout"],
)

_moby_frontend = rule(
    implementation = lambda ctx: ctx.super(),
    parent = frontend_test,
    cfg = _moby_timeout,
)

moby_go_test, _moby_go_transition = with_cfg(
    _go_test,
    test_frontend = _moby_frontend,
    extra_providers = [GoLibrary, GoArchive],
).build()

def moby_test(name, **kwargs):
    """Declares the public installed test and its owned Docker entrypoint.

    Args:
      name: Original public test target name.
      **kwargs: Shared sources, inputs, dependencies and shard count.
    """
    go_test(
        name = name,
        tags = ["external", "manual", "no-sandbox"] + local_test_tags,
        **kwargs
    )
    owned_docker_test(
        name = name,
        architectures = ["amd64"],
        cohort = "moby",
        runtime_variants = [struct(
            name = "",
            args = ["--net-raw", "--allow-packet-socket-write", "--TESTONLY-nftables"],
            test_rule = moby_go_test,
        )],
        tags = ["manual"],
        visibility = ["//:sandbox"],
        **kwargs
    )
