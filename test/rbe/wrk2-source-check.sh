set -euo pipefail
r="$RUNNER_TEMP/wrk2/licenses"; mkdir -p "$r"; exec > >(tee "$r/coordinator.log") 2>&1
git checkout -q --detach 91ee0c01fa37bdc81ed3a05050e07ffe11a03c1b
git rev-parse HEAD >"$r/source"
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
export MAKEFLAGS=DOCKER_BUILD=false
bz test //tools/licensecheck:licensecheck_test --test_filter=TestLicenseSourceIdentity/pinned.COPYRIGHT
bz build //tools/licensecheck/main:licensecheck
lic=$(bz cquery --output=files //tools/licensecheck/main:licensecheck)
"$lic" --mode=verify >"$r/verify" 2>&1
python3 - "$r" <<'PY'
from pathlib import Path
import re,sys
r=Path(sys.argv[1])
s=Path('tools/licensecheck/dependencies.yaml').read_text()
parts=re.split(r'(?m)(?=^- dependency: )',s); rows={s.splitlines()[0].removeprefix('- dependency: '):s for s in parts[1:]}
names=('luajit','openssl','wrk2','zlib')
(r/'expected').write_text(''.join(rows[n] for n in names))
(r/'regenerated.yaml').write_text(parts[0]+''.join(s for n,s in rows.items() if n not in names))
PY
"$lic" --mode=fetch --yaml="$r/regenerated.yaml" >"$RUNNER_TEMP/wrk2-fetch" 2>&1
grep -E '^(luajit|openssl|wrk2|zlib)[[:space:]]' "$RUNNER_TEMP/wrk2-fetch" >"$r/discovered"
test "$(wc -l <"$r/discovered")" = 4
if grep -i -E 'kept existing entry|failed:' "$r/discovered"; then exit 1; fi
python3 - "$r" <<'PY'
import re,sys
from pathlib import Path
r=Path(sys.argv[1])
def rows(p):
    return {s.splitlines()[0].removeprefix('- dependency: '):re.sub(r'(?m)^  retrieved: .*\n','',s) for s in re.split(r'(?m)(?=^- dependency: )',p.read_text()) if s.startswith('- dependency: ')}
want=rows(r/'expected'); got=rows(r/'regenerated.yaml')
assert len(want)==4 and want=={n:got[n] for n in want},(want,{n:got.get(n) for n in want})
PY
date -u '+PASS %FT%TZ' >"$r/verdict"
