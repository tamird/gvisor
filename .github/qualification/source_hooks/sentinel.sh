#!/bin/bash
if [[ "$HOOK_SENTINEL" == fail ]]; then
  echo "COORDINATOR_INTENTIONAL_TEST_FAILURE"
  exit 23
fi
echo "COORDINATOR_SENTINEL_PASS"
