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
    def launch(prefixes):
        manifest.write_text("".join(f"{name} {where}\n" for name, where in prefixes))
        return subprocess.run(command, env=env, cwd=root, capture_output=True)

    missing = launch([(p.name, root / "missing" / p.name) for p in repo_dirs])
    if missing.returncode == 0 or product.exists():
        raise AssertionError("Writer ran with all manifest prefixes missing")
    restored = launch([(p.name, p) for p in repo_dirs])
    if restored.returncode:
        raise RuntimeError(f"Relocated writer failed: {restored.stderr!r}")
    assert notice(product) == notice(seed)
    archive_sha = digest(product)
    payload_sha = hashlib.sha256(notice(product)).hexdigest()
    # Pure manifests need not share any runfiles tree. Record what the existing
    # Python adapter actually supports when each repository lives on its own.
    product.unlink()
    independent = []
    for index, repo in enumerate(repo_dirs):
        location = root / f"repo-{index}"
        repo.rename(location)
        independent.append((repo.name, location))
    split = launch(independent)
    if not split.returncode:
        assert digest(product) == archive_sha and notice(product) == notice(seed)
    result = {
        "writer": digest(writer),
        "interpreter": digest(Path(sys.executable)),
        "manifest_prefixes": [p.name for p in repo_dirs],
        "missing_prefix_exit": missing.returncode,
        "relocated_exit": restored.returncode,
        "relocated_archive": archive_sha,
        "payload": payload_sha,
        "independent_roots_exit": split.returncode,
        "independent_roots_mentions_runfiles": b"runfiles" in split.stderr.lower(),
    }
output.write_text(json.dumps(result, sort_keys=True) + "\n")
