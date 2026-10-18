"""Configuration of the public release artifacts and qualification test."""

load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")
load("//test/docker:defs.bzl", "docker_command_test")

# Match .buildkite/release.yaml, even when selected beside unit tests that use
# --strip=never. These settings also apply to both artifact CPUs.
release_artifacts, _artifacts_transition = with_cfg(native.filegroup).set("compilation_mode", "opt").set("strip", "sometimes").build()
# Fix the Python version so repository checks have stable artifact names without
# consulting the release registry. This setting applies only to this test.
repository_test, _repository_transition = with_cfg(docker_command_test).set("compilation_mode", "opt").set("strip", "sometimes").set(Label("//sandboxexec/sandbox/python:release_version"), "2026.10.18.1").build()
