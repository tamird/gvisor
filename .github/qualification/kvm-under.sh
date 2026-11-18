#!/bin/bash
# Root KVM access, with ownership returned before Bazel validates its outputs.
set -euo pipefail
uid=$1
gid=$2
shift 2
[[ "$uid" =~ ^[0-9]+$ && "$gid" =~ ^[0-9]+$ && "$uid" != 0 ]]
: "${TEST_UNDECLARED_OUTPUTS_DIR:?}" "${XML_OUTPUT_FILE:?}"
out=$(realpath -m "$TEST_UNDECLARED_OUTPUTS_DIR")
xml=$(realpath -m "$XML_OUTPUT_FILE")
case "$out" in
 */testlogs/test/rbe/tcg/vfio_permanent_test/test.outputs) ;;
 *) exit 2 ;;
esac
test "$xml" = "$(dirname "$out")/test.xml"
cleanup() {
 local rc=$?
 trap - EXIT TERM INT
 timeout --kill-after=2s 15s chown -R "$uid:$gid" "$out" || rc=1
 if test -f "$xml"; then timeout --kill-after=2s 5s chown "$uid:$gid" "$xml" || rc=1; fi
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
"$@"
