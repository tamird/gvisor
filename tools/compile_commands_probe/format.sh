#!/usr/bin/env bash
set -euo pipefail
cd "$BUILD_WORKSPACE_DIRECTORY"
options=$(python3 - <<'OPTIONS'
import json, os
options = json.loads(os.environ['QUALIFICATION_BAZEL_OPTIONS_JSON'])
joined = ' '.join(options)
assert joined.split() == options
print(joined)
OPTIONS
)
bash tools/lint.sh --bazel="$QUALIFICATION_BAZEL" --bazel-options="$options" buildifier spelling
