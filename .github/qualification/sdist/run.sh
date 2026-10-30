#!/bin/bash
set -euo pipefail
mode=$1
head=$2
root=$RUNNER_TEMP
out=$root/publication/$mode
mkdir -p "$out"
export PUBLICATION="$out" QUALIFICATION_COMMIT="$head"
git checkout --quiet --detach "$head"
cp "$root/row/MODULE.bazel.lock" MODULE.bazel.lock
export USE_BAZEL_VERSION
USE_BAZEL_VERSION=$(cat images/default/bazelversion)
bazelisk mod graph --lockfile_mode=update --depth=1 --output=json > "$out/module-graph.txt" 2>"$root/$mode-mod.log" || {
  tail -12 "$root/$mode-mod.log"; exit 1;
}
# Evaluating the pip hub in update mode records the selected wheel URLs before
# the established harness freezes and validates the lock on each architecture.
bazelisk query --lockfile_mode=update 'deps(//tools/python_distribution:frontend)' > "$root/$mode-query.log" 2>"$root/$mode-query.stderr" || {
  sed -nE '/^(ERROR|FATAL|WARNING):/p' "$root/$mode-query.stderr" | grep -viE 'header|token|secret|authorization|credential' | head -10 > "$out/preflight.txt" || :
  cat "$out/preflight.txt"
  exit 1
}
rm -f "$out/module-graph.txt"
sha256sum MODULE.bazel.lock | cut -d' ' -f1 > "$out/resolved-lock.sha256"
export BB_LOG_DECODER="$root/bb"
status=0
for arch in amd64 arm64; do
  export QUALIFICATION_ARCH="$arch" RUNNER_TEMP="$root/$mode-$arch"
  mkdir -p "$RUNNER_TEMP"
  args=(
    "--bazel-option=--host_platform=//tools/bazeldefs:rbe_linux_$arch"
    "--bazel-option=--extra_execution_platforms=//tools/bazeldefs:rbe_linux_$arch"
    --bazel-option=--@rules_python//python/config_settings:bootstrap_impl=native
    --bazel-option=--action_env=PATH=/usr/bin:/bin
    --bazel-option=--remote_download_outputs=toplevel
    --bazel-option=--//sandboxexec/sandbox/python:release_version=2026.10.29.2
  )
  rc=0
  bash test/rbe/actions.sh "${args[@]}" '--bazel-option=--modify_execution_info=^PythonDistribution$=+no-remote-cache' > "$RUNNER_TEMP/coordinator.log" 2>&1 || rc=$?
  echo "$rc" > "$out/$arch.exit"
  if ((rc)); then python3 "$root/sdist-fixture/diagnose.py" "$RUNNER_TEMP" "$out/$arch-diagnostics.json"; fi
  python3 test/rbe/inspect_release_wheel.py || status=1
  if ((rc != 0 && status == 0)); then status=$rc; fi
done
exit "$status"
