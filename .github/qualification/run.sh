#!/bin/bash
set -euo pipefail
: "${SOURCE:?}" "${QUAL_CONFIG:?}" "${QUAL_ARCH:?}" "${RESULT_DIR:?}"
test "$SOURCE" = bf5df9a1c9211afe4dbd7267c4423e9ae575f7fa
test "$QUAL_CONFIG" = aarch64
test "$QUAL_ARCH" = arm64
test "$(git rev-parse HEAD)" = "$SOURCE"
mkdir -p "$RESULT_DIR"
flags=(--config=aarch64 --jobs=3 --strip=never --nostamp --remote_executor= --remote_cache= --bes_backend=)
command=(make DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}" lint-cc)
printf '%q ' "${command[@]}" > "$RESULT_DIR/base-lint-cc.command"
printf '\n' >> "$RESULT_DIR/base-lint-cc.command"
printf '%s\n' "$SOURCE" > "$RESULT_DIR/source.txt"
uname -a > "$RESULT_DIR/worker.txt"
rc=0
"${command[@]}" 2>&1 | tee "$RESULT_DIR/base-lint-cc.log" || rc=$?
printf '%s\tbase-lint-cc\t%s\n' "$SOURCE" "$rc" > "$RESULT_DIR/commands.tsv"
python3 - "$RESULT_DIR" "$SOURCE" "$rc" <<'PYOUT'
import json
from pathlib import Path
import sys
root=Path(sys.argv[1]);rc=int(sys.argv[3])
(root/'baseline-control-outcome.json').write_text(json.dumps({'source':sys.argv[2],'architecture':'arm64','command':'base-lint-cc','exitCode':rc,'status':'passed' if rc==0 else 'not-passed'},indent=2)+'\n')
PYOUT
exit "$rc"
