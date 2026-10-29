#!/bin/bash
set -eu
case "${TEST_TARGET:?}" in
  //runsc/cmd/alias/bwrap:bwrap_test|//runsc/cmd/alias/bwrap:bwrap_integration_test) ;;
  *) exit 1 ;;
esac
xml=${XML_OUTPUT_FILE:?}
case "$xml" in
  /*/test.xml) ;;
  *) echo "unexpected XML_OUTPUT_FILE" >&2; exit 1 ;;
esac
proof="${BWRAP_XML_PROOF:?}/${TEST_TARGET##*:}-ownership.txt"
uid=$(id -u)
gid=$(id -g)
test "$uid" -ne 0
printf 'target=%s\nrunner_uid=%s\nrunner_gid=%s\n' "$TEST_TARGET" "$uid" "$gid" > "$proof"
status=0
sudo -n -E -- "$@" || status=$?
printf 'test_exit=%d\n' "$status" >> "$proof"

# rules_go creates its real report while privileged; leave it in place and
# return ownership before Bazel inspects its permissions. Output collection
# must never replace a failed test's exit code or manufacture an XML file.
restore_report() {
  [[ -f "$xml" && ! -L "$xml" ]] || return 1
  stat -c 'before: owner=%u group=%g mode=%a bytes=%s' "$xml" >> "$proof" || return 1
  local mode_status=0
  chmod --reference="$xml" -- "$xml" 2>> "$proof" || mode_status=$?
  printf 'runner_chmod_before=%d\n' "$mode_status" >> "$proof" || return 1
  sudo -n chown --no-dereference -- "$uid:$gid" "$xml" || return 1
  stat -c 'after: owner=%u group=%g mode=%a bytes=%s' "$xml" >> "$proof" || return 1
  chmod --reference="$xml" -- "$xml" || return 1
  printf 'runner_chmod_after=0\n' >> "$proof"
}

restore=0
restore_report || restore=$?
if ((status)); then exit "$status"; fi
exit "$restore"
