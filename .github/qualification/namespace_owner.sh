#!/bin/bash
set -euo pipefail
trap 'echo "Fixture failed on line $LINENO: $BASH_COMMAND" >&2' ERR
program="$1"
name="$2"
private="$(mktemp -d -- "$GUEST_TMP/guest.XXXXXX")"
chmod 0755 "$private"
# runsc locates sidecars next to its own executable. Copy the public release
# as a unit to dereference Bazel's runfiles symlinks.
cp -aL "$TEST_SRCDIR/$TEST_WORKSPACE/release" "$private/release"
install -m755 "$program" "$private/gtest"
test -x "$private/release/runsc"
test -x "$private/release/gvisor-bin/gvisor_sentry"
test -x "$private/gtest"
readelf -h "$private/gtest" | grep 'Machine:.*X86-64'
readelf -h "$private/release/runsc" | grep 'Machine:.*X86-64'
interp="$(readelf -W -l "$private/gtest" | sed -n 's/.*Requesting program interpreter: \(.*\)]/\1/p')"
if [[ -n "$interp" ]]; then
  test -x "$interp"
  ldd "$private/gtest" > "$private/loader"
  if grep -q 'not found' "$private/loader"; then cat "$private/loader"; exit 1; fi
fi
"$private/gtest" --gtest_list_tests "--gtest_filter=$name" | grep -F -- "${name#*.}"
sha256sum "$private"/{gtest,release/runsc,release/gvisor-bin/gvisor_sentry}
mkdir "$private/log"
# Only the runtime uses sudo. Bazel owns its test outputs.
runtime=(sudo -n env -u XML_OUTPUT_FILE -u TEST_UNDECLARED_OUTPUTS_DIR -u TEST_TMPDIR -u TEST_SRCDIR
  TEST_ON_GVISOR=systrap GVISOR_RUNTIME=runsc GVISOR_NETWORK=none
  "$private/release/runsc" --platform=systrap --network=none "--root=$private/state" "--debug-log=$private/log/" --ignore-cgroups)
"${runtime[@]}" do /bin/cat /proc/self/status > "$private/status"
python3 - "$private/status" <<'PY'
from pathlib import Path
import sys
bits = int(next(line.split()[1] for line in Path(sys.argv[1]).read_text().splitlines() if line.startswith("CapEff:")), 16)
assert bits & (1<<12) and bits & (1<<21), hex(bits)
print("Verified guest CAP_NET_ADMIN and CAP_SYS_ADMIN")
PY
set +e
"${runtime[@]}" do "$private/gtest" "--gtest_filter=$name"
status=$?
set -e
echo "Guest exit: $status"
if sudo -n grep -RFsl -- 'registerChild called on a non-root namespace' "$private/log" >/dev/null; then
  echo 'Sentry: registerChild called on a non-root namespace'
fi
exit "$status"
