#!/bin/bash
set -euo pipefail
inputs="${TEST_SRCDIR:?}/${TEST_WORKSPACE:?}/.github/qualification/unit-cgroup-v1"
out="${TEST_UNDECLARED_OUTPUTS_DIR:?}"
unset CGROUPV2
# The measured block is copied verbatim from the product hook.
source "$inputs/hook.bash"
[[ $CGROUPV2 == false && $(id -u) != 0 ]]
printf '%s\n' "$CGROUPV2" "$(id -u)" "$(uname -r)" > "$out/owner-mode"
sha256sum "$inputs/hook.bash" "$inputs/selected-test" | cut -d' ' -f1 > "$out/owner-inputs.sha256"
exec "$inputs/selected-test" "$@"
