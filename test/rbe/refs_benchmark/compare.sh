#!/bin/bash
# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail
[[ $# == 6 ]]
root=$PWD
before=$root/$1
after=$root/$2
manifest=$root/$3
benchstat=$root/$4
nm=$root/$5
objdump=$root/$6
out=$TEST_UNDECLARED_OUTPUTS_DIR
work=$TEST_TMPDIR/refs-comparison
mkdir "$work"
(cd "$(dirname "$before")" && sha256sum --check "$manifest")

# These are two builds of the existing benchmark, not two benchmark variants.
for side in before after; do
  mkdir "$work/$side"
  archive=$before
  [[ $side == before ]] || archive=$after
  tar -xf "$archive" -C "$work/$side"
  (cd "$work/$side" && sha256sum --check SHA256SUMS) > "$out/$side-input-check.txt"
  sha256sum "$work/$side/kernel_test" > "$out/$side-binary.sha256"
  "$nm" --defined-only --print-size --format=bsd "$work/$side/kernel_test" > "$work/$side-symbols.txt"
  : > "$out/$side-selected-symbols.txt"
  : > "$out/$side-disassembly.txt"
  count=0
  bytes=0
  while read -r address size kind symbol; do
    [[ $kind == T || $kind == t ]] || continue
    case "$symbol" in
      *BenchmarkFDLookupAndDecRef*|*'(*FDTable).Get'|*'(*FileDescription).DecRef'*|*'(*View).Clone'*) ;;
      *RefsBase*|*fileDescriptionRefs*|*chunkRefs*)
        case "$symbol" in
          *.IncRef|*.TryIncRef|*.DecRef|*.LogRefs) ;;
          *) continue ;;
        esac ;;
      *) continue ;;
    esac
    [[ $address =~ ^[[:xdigit:]]+$ && $size =~ ^[[:xdigit:]]+$ ]]
    count=$((count + 1))
    bytes=$((bytes + 16#$size))
    ((count <= 512 && bytes <= 1048576))
    printf '%s %s %s %s\n' "$address" "$size" "$kind" "$symbol" >> "$out/$side-selected-symbols.txt"
    "$objdump" --disassemble --line-numbers --symbolize-operands \
      --start-address="0x$address" --stop-address="$((16#$address + 16#$size))" \
      "$work/$side/kernel_test" >> "$out/$side-disassembly.txt"
  done < "$work/$side-symbols.txt"
  ((count > 0))
  grep -F 'BenchmarkFDLookupAndDecRef' "$out/$side-selected-symbols.txt" >/dev/null
  [[ $(wc -c < "$out/$side-disassembly.txt") -le 16777216 ]]
done

# Capture relevant worker facts without exporting its whole environment.
uname -srmo > "$out/worker.txt"
grep -E '^(model name|cpu family|model|stepping|flags)[[:space:]]*:' /proc/cpuinfo | sort -u >> "$out/worker.txt"
grep -E '^(Cpus_allowed_list|Mems_allowed_list):' /proc/self/status >> "$out/worker.txt"
"$nm" --version > "$out/nm-version.txt"
"$objdump" --version > "$out/objdump-version.txt"

run_sample() {
  local side=$1 sample=$2 log=$out/$1-$2.txt
  # Keep the existing small Go owner's deadline. The outer medium owner also
  # bounds the full comparison, including disassembly and all subprocesses.
  env -u TESTBRIDGE_TEST_ONLY -u XML_OUTPUT_FILE -u TEST_TOTAL_SHARDS \
    -u TEST_SHARD_INDEX -u TEST_SHARD_STATUS_FILE \
    GO_TEST_WRAP=0 GO_TEST_RUN_FROM_BAZEL=1 GOMAXPROCS=1 TEST_TIMEOUT=60 \
    TEST_SRCDIR="$work/$side/kernel_test.runfiles" TEST_WORKSPACE=_main \
    "$work/$side/kernel_test" -test.run='^$' \
    -test.bench='^BenchmarkFDLookupAndDecRef$' -test.benchmem \
    -test.benchtime=1s -test.count=1 -test.cpu=1 > "$log" 2>&1
  [[ $(grep -Ec '^BenchmarkFDLookupAndDecRef(-1)?[[:space:]]+[0-9]+' "$log") == 1 ]]
  grep -x PASS "$log" >/dev/null
  printf '%s %s\n' "$sample" "$side" >> "$out/order.txt"
  if [[ $sample != warmup ]]; then
    cat "$log" >> "$out/$side.txt"
  fi
}
run_sample before warmup
run_sample after warmup
: > "$out/before.txt"
: > "$out/after.txt"
for block in 1 2 3 4 5; do
  run_sample before "$block-1"
  run_sample after "$block-2"
  run_sample after "$block-3"
  run_sample before "$block-4"
done
"$benchstat" "$out/before.txt" "$out/after.txt" > "$out/benchstat.txt"
cat "$out/benchstat.txt"
printf 'REFS_COMPARISON_COMPLETE samples_per_binary=10\n'
