"""Configuration of the public release artifacts and qualification test."""

load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")
load("//test/docker:defs.bzl", "docker_command_test")

# Keep the public release build's default stripping policy when selected beside
# unit tests, which use --strip=never. This also applies to both artifact CPUs.
release_artifacts, _artifacts_transition = with_cfg(native.filegroup).set("strip", "sometimes").build()
repository_test, _repository_transition = with_cfg(docker_command_test).set("strip", "sometimes").build()
