"""Configuration of the public release artifacts and qualification test."""

load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")
load("//test/docker:defs.bzl", "docker_command_test")

# Match .buildkite/release.yaml, even when selected beside unit tests that use
# --strip=never. These settings also apply to both artifact CPUs.
release_artifacts, _artifacts_transition = with_cfg(native.filegroup).set("compilation_mode", "opt").set("strip", "sometimes").build()
repository_test, _repository_transition = with_cfg(docker_command_test).set("compilation_mode", "opt").set("strip", "sometimes").build()
