#!/bin/bash
# Preserve native capabilities and return exact declared outputs to Bazel's uid.
set -euo pipefail
uid=$1;gid=$2;shift 2
[[ "$uid" =~ ^[0-9]+$ && "$gid" =~ ^[0-9]+$ && "$uid" != 0 ]]
case "${TEST_TARGET:-}" in
 //runsc/gofer/fs:lisafs_test) relative=runsc/gofer/fs/lisafs_test ;;
 //runsc/gofer/fs:groups_test) relative=runsc/gofer/fs/groups_test ;;
 //test/runner:runner_test) relative=test/runner/runner_test ;;
 *) exit 2 ;;
esac
: "${TEST_UNDECLARED_OUTPUTS_DIR:?}" "${XML_OUTPUT_FILE:?}"
out=$(realpath -m "$TEST_UNDECLARED_OUTPUTS_DIR")
xml=$(realpath -m "$XML_OUTPUT_FILE")
case "$out" in */testlogs/"$relative"/test.outputs) ;; *) exit 2 ;; esac
test "$xml" = "$(dirname "$out")/test.xml"
cleanup() {
 local rc=$?
 trap - EXIT TERM INT
 if test -d "$out";then timeout --kill-after=2s 15s chown -hR "$uid:$gid" "$out" || rc=1;fi
 if test -s "$xml";then timeout --kill-after=2s 5s chown -h "$uid:$gid" "$xml" || rc=1;else echo 'Native test omitted its actual XML before output validation' >&2;rc=1;fi
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
"$@"
