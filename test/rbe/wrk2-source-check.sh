set -euo pipefail
r="$RUNNER_TEMP/wrk2/licenses"; mkdir -p "$r"; exec > >(tee "$r/coordinator.log") 2>&1
git checkout -q --detach 5bb80cbb9c07ec3f597f3e5200102b6c8e0a95f4
git rev-parse HEAD >"$r/source"
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
export MAKEFLAGS=DOCKER_BUILD=false
bz build //tools/licensecheck/main:licensecheck
lic=$(bz cquery --output=files //tools/licensecheck/main:licensecheck)
stock_rc=0
"$lic" --mode=verify >"$r/verify" 2>&1 || stock_rc=$?
echo "$stock_rc" >"$r/checked-in-verify-rc"
python3 - "$r" <<'PY'
from pathlib import Path
import re,sys
r=Path(sys.argv[1])
s=Path('tools/licensecheck/dependencies.yaml').read_text()
parts=re.split(r'(?m)(?=^- dependency: )',s); rows={s.splitlines()[0].removeprefix('- dependency: '):s for s in parts[1:]}
names=('luajit','openssl','wrk2')
(r/'expected').write_text(''.join(rows[n] for n in names))
(r/'regenerated.yaml').write_text(parts[0]+''.join(s for n,s in rows.items() if n not in names))
PY
fetch_rc=0
"$lic" --mode=fetch --yaml="$r/regenerated.yaml" >"$r/fetch.log" 2>&1 || fetch_rc=$?
echo "$fetch_rc" >"$r/fetch-rc"
fresh_rc=0
"$lic" --mode=verify --yaml="$r/regenerated.yaml" >"$r/fresh-verify" 2>&1 || fresh_rc=$?
echo "$fresh_rc" >"$r/fresh-verify-rc"
grep -E '^(luajit|openssl|wrk2)[[:space:]]' "$r/fetch.log" >"$r/discovered"
test "$(wc -l <"$r/discovered")" = 3
if grep -i -E 'kept existing entry|failed:' "$r/discovered"; then exit 1; fi
python3 - "$r" <<'PY'
import re,sys
from pathlib import Path
r=Path(sys.argv[1])
def rows(p):
    return {s.splitlines()[0].removeprefix('- dependency: '):re.sub(r'(?m)^  retrieved: .*\n','',s) for s in re.split(r'(?m)(?=^- dependency: )',p.read_text()) if s.startswith('- dependency: ')}
want=rows(r/'expected'); got=rows(r/'regenerated.yaml')
assert len(want)==3 and want=={n:got[n] for n in want},(want,{n:got.get(n) for n in want})
PY
test "$stock_rc" = 0
test "$fetch_rc" = 0
test "$fresh_rc" = 0
date -u '+THREE EXACT DISCOVERY ROWS PASSED %FT%TZ' >"$r/verdict"
