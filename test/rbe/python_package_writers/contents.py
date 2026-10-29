"""Read the bytes from the selected Bazel actions and any finished packages."""

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

out = Path(sys.argv[1])
expected = Path("test/rbe/python_package_writers/notice.txt").read_bytes()
result = {"packages": []}


def verify_tar(raw):
    with tarfile.open(fileobj=io.BytesIO(raw)) as t:
        found = [m for m in t if m.name.lstrip("./") == "share/notice.txt"]
        assert len(found) == 1 and t.extractfile(found[0]).read() == expected


def members(data):
    assert data.startswith(b"!<arch>\n")
    result = {}
    pos = 8
    while pos < len(data):
        h = data[pos:pos+60]
        assert h[-2:] == b"`\n"
        n = int(h[48:58]); pos += 60
        key = h[:16].decode().strip().rstrip("/")
        assert key not in result
        result[key] = data[pos:pos+n]; pos += n + n % 2
    assert pos == len(data)
    return result


expected_outputs = {
    ".tar": (10240, "3a57a5fe4121f867ccd0c26c89c511dd0f5851712ae6723babed02f950193898"),
    ".deb": (572, "486d93a4ea624b12536af762811caeac49ab801c05169c7dc744d737673ab015"),
}
base = Path("bazel-bin/test/rbe/python_package_writers")
matches = [base / "payload.tar", *base.glob("*.deb")]
# The declared name and canonical Debian filename may be symlinks to the same bytes.
packages = {}
for p in matches:
    b = p.read_bytes(); packages.setdefault(hashlib.sha256(b).hexdigest(), (p.name, b))
assert len(packages) == 2, list(packages)
for digest, (name, raw) in packages.items():
    if name.endswith(".tar"):
        verify_tar(raw)
    else:
        ar = members(raw)
        assert ar["debian-binary"] == b"2.0\n"
        verify_tar(next(v for k,v in ar.items() if k.startswith("data.tar")))
        with tarfile.open(fileobj=io.BytesIO(next(v for k,v in ar.items() if k.startswith("control.tar")))) as t:
            c = next(m for m in t if m.name.lstrip("./") == "control")
            fields = t.extractfile(c).read().decode().splitlines()
            assert all(field in fields for field in ["Package: gvisor-writer-input", "Version: 1", "Architecture: all"]), fields
    kind = ".tar" if name.endswith(".tar") else ".deb"
    assert (len(raw), digest) == expected_outputs[kind], name
    result["packages"].append({"name":name,"size":len(raw),"sha256":digest})
(out / "contents.json").write_text(json.dumps(result, indent=2) + "\n")
