#!/bin/bash
set -eu -o pipefail
dist=$1
shift
mkdir -p "$TEST_TMPDIR/wheelhouse"
for file in "$@"; do cp "$file" "$TEST_TMPDIR/wheelhouse/"; done
python=/usr/local/bin/python3.7
"$python" -c 'import sys; assert sys.version_info[:3] == (3, 7, 17), sys.version'
"$python" -m pip --version
for artifact in "$dist"/*.whl "$dist"/*.tar.gz; do
  file=$(basename "$artifact")
  target="$TEST_TMPDIR/$file-installed"
  "$python" -m pip --disable-pip-version-check install --no-cache-dir --no-index \
    --no-deps --find-links="$TEST_TMPDIR/wheelhouse" --target="$target" "$artifact"
  PYTHONPATH="$target" "$python" - "$artifact" "$target" <<'PY'
import hashlib, json, pathlib, sys
import gvisor
from gvisor import sandbox
artifact, target = map(pathlib.Path, sys.argv[1:])
assert pathlib.Path(gvisor.__file__).resolve() == target / "gvisor" / "__init__.py"
assert gvisor.Sandbox is sandbox.Sandbox and gvisor.MountType.BIND.value == "bind"
print("SDIST_INSTALL " + json.dumps({"artifact": artifact.name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    "python": sys.version.split()[0], "modules": sorted(p.name for p in (target / "gvisor").glob("*.py"))}))
PY
done
