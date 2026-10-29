"""Exercise the actual tar writer from another directory using only manifest prefixes."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def notice(path):
    with tarfile.open(path) as archive:
        entry, = [m for m in archive if m.name.lstrip("./") == "share/notice.txt"]
        return archive.extractfile(entry).read()


writer, seed, output = (Path(arg).absolute() for arg in sys.argv[1:])
assert writer.read_bytes().startswith(b"\x7fELF")
with tempfile.TemporaryDirectory(dir=output.parent) as work:
    root = Path(work)
    binary = root / "entry"
    shutil.copy(writer, binary)
    assert not Path(str(binary) + ".runfiles").exists()
    runfiles = root / "moved.runfiles"
    shutil.copytree(Path(str(writer) + ".runfiles"), runfiles)
    repo_dirs = sorted(p for p in runfiles.iterdir() if p.is_dir())
    assert len(repo_dirs) >= 2
    manifest = root / "names.runfiles_manifest"
    env = {"RUNFILES_MANIFEST_FILE": str(manifest), "PATH": "/no-host-tools"}
    product = root / "output.tar"
    command = [str(binary), "--output", str(product), "--tar", str(seed), "--mtime", "portable"]
    # Each key denotes a whole repository; the manifest never names the
    # interpreter, the generated Python adapter, or any individual source file.
    def launch(where):
        manifest.write_text("".join(f"{p.name} {where / p.name}\n" for p in repo_dirs))
        return subprocess.run(command, env=env, cwd=root, capture_output=True)

    missing = launch(root / "missing")
    if missing.returncode == 0 or product.exists():
        raise AssertionError("Writer ran with all manifest prefixes missing")
    restored = launch(runfiles)
    if restored.returncode:
        raise RuntimeError(f"Relocated writer failed: {restored.stderr!r}")
    assert notice(product) == notice(seed)
    result = {
        "writer": digest(writer),
        "interpreter": digest(Path(sys.executable)),
        "manifest_prefixes": [p.name for p in repo_dirs],
        "missing_prefix_exit": missing.returncode,
        "relocated_exit": restored.returncode,
        "relocated_archive": digest(product),
        "payload": hashlib.sha256(notice(product)).hexdigest(),
    }
output.write_text(json.dumps(result, sort_keys=True) + "\n")
