#!/bin/sh
# CI-only canonical formatting of the instrumentation proposal.
set -eu
output=$("$1" -format-only -d "$3" "$4")
if [ -n "$output" ]; then
    printf '%s\n' "$output"
    exit 1
fi
"$2" --mode=diff --lint=warn --warnings=-duplicated-name,-list-append,-native-py,-native-sh-binary,-native-sh-library,-native-sh-test,-native-proto "$5"
