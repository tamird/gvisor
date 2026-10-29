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


def copy_runfiles(source, target):
    def copy(src, dst):
        if src.is_symlink():
            link = Path(os.path.normpath(src.parent / os.readlink(src)))
            if link.is_relative_to(source):
                dst.symlink_to(os.path.relpath(target / link.relative_to(source), dst.parent))
                return
        if src.is_dir():
            dst.mkdir()
            for item in src.iterdir():
                copy(item, dst / item.name)
        else:
            shutil.copy2(src, dst)

    copy(source, target)
    # In particular, the generated venv interpreter is a link to the toolchain
    # inside the moved tree. Turning it into a regular file loses CPython's prefix.
    links = {p.relative_to(target): p.resolve(strict=True).relative_to(target)
             for p in target.rglob("*") if p.is_symlink()}
    assert any(p.parent.name == "bin" and p.name == "python3.11" for p in links), links
    return links


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
    links = copy_runfiles(Path(str(writer) + ".runfiles"), runfiles)
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
    independent = {}
    for index, repo in enumerate(sorted(runfiles.iterdir())):
        location = root / f"repo-{index}"
        repo.rename(location)
        independent[repo.name] = location
    # Preserve link targets across repos now that the repository roots move
    # independently. A failure must not be an accidental broken venv link.
    for name, where in links.items():
        link = independent[name.parts[0]].joinpath(*name.parts[1:])
        dest = independent[where.parts[0]].joinpath(*where.parts[1:])
        link.unlink()
        link.symlink_to(os.path.relpath(dest, link.parent))
        assert link.resolve(strict=True) == dest.resolve(strict=True)
    split = launch([(p.name, independent[p.name]) for p in repo_dirs])
    if not split.returncode:
        assert digest(product) == archive_sha and notice(product) == notice(seed)
    result = {
        "writer": digest(writer),
        "interpreter": digest(Path(sys.executable)),
        "internal_links_preserved": len(links),
        "manifest_prefixes": [p.name for p in repo_dirs],
        "missing_prefix_exit": missing.returncode,
        "relocated_exit": restored.returncode,
        "relocated_archive": archive_sha,
        "payload": payload_sha,
        "independent_roots_exit": split.returncode,
        "independent_roots_adapter_missing_root": b"Cannot find application runfiles for " in split.stderr,
    }
output.write_text(json.dumps(result, sort_keys=True) + "\n")
