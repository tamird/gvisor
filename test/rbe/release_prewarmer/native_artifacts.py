"""Inspect the existing native test wrapper and worker image evidence."""

import hashlib
import io
import json
import re
import subprocess
import zipfile

import qualify as q



def elf(path, cpu):
    assert path.is_file() and path.stat().st_size <= 200 * 1024 * 1024
    data = path.read_bytes()
    result = q.elf_bytes(data)
    assert result["load"] and result["machine"] == {"amd64": 62, "arm64": 183}[cpu]
    output = subprocess.check_output(["readelf", "-lWd", str(path)], text=True, timeout=45)
    interpreter = re.findall(r"Requesting program interpreter: ([/A-Za-z0-9_.+-]{1,160})\]", output)
    needed = re.findall(r"\(NEEDED\).*?\[([A-Za-z0-9+_.-]{1,100})\]", output)
    assert len(interpreter) == int(result["interp"]) and len(needed) == output.count("(NEEDED)")
    return dict(result, interpreter=interpreter, needed=needed)


def image(arch):
    raw = (q.TMP / "images.zip").read_bytes()
    assert len(raw) == 4965 and hashlib.sha256(raw).hexdigest() == "5f919ba8d820862f00522ab10658beb6738f31c12550c1cda1d8e15ce5973859"
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        doc = json.loads(z.read(arch + "-image.json"))
    assert doc["manifest"] == q.PIN[arch] and doc["architecture"] == arch and doc["elf_paths"] == []
    return {"original_artifact": 13100242102, "manifest": doc["manifest"], "elf_paths": []}
