#!/bin/bash
set -eu
name=$1
shift
file=$(mktemp "$VERSION_TEMP/native/$name-XXXXXXXX")
exec bazel --bazelrc="$VERSION_TEMP/version.bazelrc" "$name" --build_event_json_file="$file" "$@"
