#!/bin/bash
# Bounded helper-access diagnosis only; no TCP transfer or benchmark.
set -euo pipefail
: "${SOURCE:?}" "${BASE:?}" "${RUNNER_TEMP:?}" "${RESULT_DIR:?}" "${GITHUB_RUN_ID:?}"
test "$SOURCE" = f9446ddf973def4ce653acbb098532c2b1eb0ef0
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git rev-parse HEAD)" = "$SOURCE"
support=$(cd "$(dirname "$0")" && pwd)
w="$RUNNER_TEMP/tcp-loss-pilot"
alternative="/var/tmp/gvisor-tcp-loss-$GITHUB_RUN_ID"
mkdir -m 711 "$w"
mkdir -m 755 "$w/base"
cleanup() {
 local rc=$?
 trap - EXIT
 rm -rf -- "$w"
 if test "${alternative_created:-0}" = 1; then rm -rf -- "$alternative"; fi
 exit "$rc"
}
trap cleanup EXIT
for tool in namei findmnt python3 timeout; do command -v "$tool"; done
namei -l "$w/base/nsjoin" > "$RESULT_DIR/namei-before.txt" 2>&1 || true
findmnt -T "$w" -o TARGET,SOURCE,FSTYPE,OPTIONS > "$RESULT_DIR/mount-before.txt"
# The probe enters via an already-open stdin descriptor, so access to the
# private plan directory does not itself prevent namespace diagnostics.
probe_contexts() {
 local prefix="$1" helper="$2"
 timeout --kill-after=5s 30s python3 - "$helper" < "$support/probe.py" > "$RESULT_DIR/$prefix-host.json"
 timeout --kill-after=5s 30s sudo python3 - "$helper" < "$support/probe.py" > "$RESULT_DIR/$prefix-sudo.json"
 timeout --kill-after=5s 30s sudo unshare --user --map-root-user --net python3 - "$helper" < "$support/probe.py" > "$RESULT_DIR/$prefix-userns.json" 2> "$RESULT_DIR/$prefix-userns.stderr"
}
probe_contexts before "$w/base/nsjoin"
timeout --kill-after=30s 10m make DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' 'BAZEL_OPTIONS=--config=x86_64 -c opt --jobs=3 --remote_executor= --remote_cache= --bes_backend=' copy TARGETS=//test/benchmarks/tcp:nsjoin "DESTINATION=$w/base/nsjoin" > "$RESULT_DIR/nsjoin-build.log" 2>&1
chmod 755 "$w/base/nsjoin"
sha256sum "$w/base/nsjoin" > "$RESULT_DIR/nsjoin.sha256"
namei -l "$w/base/nsjoin" > "$RESULT_DIR/namei-built.txt"
probe_contexts built "$w/base/nsjoin"
# Select an alternative only after actual namespace observations establish a
# nontraversable ancestor with no applicable DAC execute permission.
if python3 - "$RESULT_DIR/built-userns.json" > "$RESULT_DIR/traversal-evidence.json" <<'PROOF'
import json
from pathlib import Path
import sys

record = json.loads(Path(sys.argv[1]).read_text())
groups = {record['gid'], *record['groups']}
blocked = [entry for entry in record['ancestors'] if entry.get('traversable') is False and 'mode' in entry and not (
    entry['mode'] & (0o100 if entry['uid'] == record['euid'] else 0o010 if entry['gid'] in groups else 0o001))]
print(json.dumps({'blockedAncestors': blocked}, indent=2))
raise SystemExit(0 if blocked and record['true'].get('returncode') == 0 and record['helper'].get('errno') == 13 else 1)
PROOF
then
 mkdir -m 711 "$alternative"
 alternative_created=1
 cp "$w/base/nsjoin" "$alternative/nsjoin"
 chmod 755 "$alternative/nsjoin"
 sha256sum "$alternative/nsjoin" > "$RESULT_DIR/alternative-nsjoin.sha256"
 cmp "$w/base/nsjoin" "$alternative/nsjoin"
 namei -l "$alternative/nsjoin" > "$RESULT_DIR/namei-alternative.txt"
 findmnt -T "$alternative" -o TARGET,SOURCE,FSTYPE,OPTIONS > "$RESULT_DIR/mount-alternative.txt"
 probe_contexts alternative "$alternative/nsjoin"
else
 printf '%s\n' 'No proven DAC traversal cause; no relocation or benchmark attempted.' > "$RESULT_DIR/no-alternative.txt"
fi
# Both original failed benchmark attempts remain evidence. This plan performs
# only host access diagnostics and does not clear any performance gate.
