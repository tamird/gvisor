#!/usr/bin/env bash
set -euo pipefail
tool=$(realpath "$1")
docker run --rm --network=none -v "$tool:/usr/bin/bazel-compile-commands:ro" ubuntu:22.04 /usr/bin/bazel-compile-commands --help
