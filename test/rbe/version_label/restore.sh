#!/bin/bash
set -euo pipefail
if [[ ${1:-} != --private ]]; then
  exec unshare --mount --propagation private -- "$0" --private "$@"
fi
shift
[[ $# == 4 && $EUID == 0 && $(uname -m) == x86_64 ]]
guest=$(realpath "$1")
release=$(realpath "$2")
ordinary=$(realpath "$3")
b=$(cat "$4")
a=release-20261029.0
[[ "$a" != "$b" && -n "$b" ]]
d=$(mktemp -d /tmp/gvisor-version-XXXXXX)
mkdir "$d"/{a,b,bundle,image,ra,rb}
for arm in a b; do
  if [[ $arm == a ]]; then src=$release; want=$a; else src=$ordinary; want=$b; fi
  tar -xjf "$src" -C "$d/$arm"
  [[ -x "$d/$arm/runsc" && -x "$d/$arm/gvisor-bin/gvisor_sentry" ]]
  "$d/$arm/runsc" --version | tee "$d/version"
  grep -Fx "runsc version $want" "$d/version"
done
mkdir "$d/bundle/rootfs"
cp "$guest" "$d/bundle/rootfs/guest"
cd "$d/bundle"
a_run="$d/a/runsc"
"$a_run" spec
python3 - <<'PY'
import json
from pathlib import Path
p=Path("config.json"); spec=json.loads(p.read_text())
spec["root"]={"path":"rootfs","readonly":True}
spec["process"].update({"args":["/guest"],"terminal":False,"cwd":"/"})
p.write_text(json.dumps(spec))
PY
run() {
  local arm=$1; shift
  GVISOR_SIDECAR_BINARIES_DIR="$d/$arm/gvisor-bin" "$d/$arm/runsc" --root="$d/r$arm" --network=none --platform=systrap --ignore-cgroups "$@"
}
trap 'run a delete --force version-case >/dev/null 2>&1 || true; run b delete --force version-case >/dev/null 2>&1 || true' EXIT
run a create --bundle "$d/bundle" version-case
run a start version-case
run a checkpoint --image-path "$d/image" version-case
[[ -s "$d/image/checkpoint.img" ]]
run a delete --force version-case
if run b restore --detach --bundle "$d/bundle" --image-path "$d/image" version-case >"$d/mismatch" 2>&1; then
  echo 'mismatched restore succeeded' >&2; exit 1
fi
grep -F 'runsc version does not match across checkpoint restore' "$d/mismatch" | grep -F "$a" | grep -F "$b"
run a restore --detach --bundle "$d/bundle" --image-path "$d/image" version-case
run a exec version-case /guest prove | grep -Fx 'restored guest executed'
# Exercise the actual first-entry release policy on the packaged Sentry.
if GVISOR_ENFORCE_RELEASE="$b" "$d/a/gvisor-bin/gvisor_sentry" boot >"$d/sidecar" 2>&1; then
  echo 'mismatched Sentry succeeded' >&2; exit 1
fi
grep -F "this binary's build" "$d/sidecar" | grep -F "$a" | grep -F "$b"
printf 'VERSION-CHECK release=%s ordinary=%s checkpoint=accepted mismatched-checkpoint=rejected matched-sidecar=spawned mismatched-sidecar=rejected\n' "$a" "$b"
