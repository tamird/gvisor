#!/usr/bin/env bash
set -e
if [[ "${1-}" == fail ]]; then exit 1; fi
sudo -n sh -c 'umask 022; echo data > "$1"' _ "$TEST_UNDECLARED_OUTPUTS_DIR/root-owned"
stat -c 'OUTPUT uid=%u mode=%a' "$TEST_UNDECLARED_OUTPUTS_DIR/root-owned"
echo BODY_EXIT=0
