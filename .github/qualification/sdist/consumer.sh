#!/bin/bash
set -euo pipefail
src=$PWD
product=a6a356a0593de1ebaefdb917144ecbe772eeaccf
export USE_BAZEL_VERSION
USE_BAZEL_VERSION=$(cat images/default/bazelversion)
data="$RUNNER_TEMP/consumer"
out="$RUNNER_TEMP/publication"
mkdir -p "$data"
cd "$data"
printf 'module(name = "wheel_consumer")
bazel_dep(name = "gvisor", version = "0.0.1")
' >MODULE.bazel
for name in stock candidate; do
  if [[ $name == stock ]]; then ref=1398e3d66; else ref="$product"; fi
  git -C "$src" checkout --quiet --detach "$ref"
  rc=0
  bazelisk query --ignore_dev_dependency --override_module="gvisor=$src" 'labels(srcs, @gvisor//sandboxexec/sandbox/python:sandbox_py)' >"$out/consumer-$name.srcs" 2>"$data/$name.log" || rc=$?
  if [[ $rc == 0 ]]; then
    for source in gvisor/__init__.py gvisor/sandbox.py; do
      grep -Fq ":$source" "$out/consumer-$name.srcs" || rc=1
    done
  fi
  printf '%s
' "$rc" >"$out/consumer-$name.exit"
  python3 - "$name" "$data/$name.log" "$out/consumer-$name.txt" <<'PYSRC'
import re,sys
from pathlib import Path
text=Path(sys.argv[2]).read_text(errors="replace")
rows=[l[:350] for l in text.splitlines() if re.search(r'ERROR:|FATAL:|Caused by:|not found|does not exist',l) and not re.search(r'http|token|header|secret|credential',l,re.I)]
Path(sys.argv[3]).write_text("\n".join(rows[:12])+"\n")
PYSRC
done
git -C "$src" checkout --quiet --detach "$product"
test "$(cat "$out/consumer-stock.exit")" = 0 && test "$(cat "$out/consumer-candidate.exit")" = 0
