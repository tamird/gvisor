import json
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

root = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]
target = root / "test/rbe/python_release_isolation"
tmp = Path(os.environ["TEST_TMPDIR"])
receipt = []
for kind in ("fixed",):
    for tool in ("build", "uv"):
        workspace = tmp / kind / tool
        project = workspace / "sandboxexec/sandbox/python"
        shutil.copytree(root / "sandboxexec/sandbox/python", project, symlinks=False)
        entry = workspace / "tools/make_python_release.sh"
        entry.parent.mkdir()
        shutil.copyfile(target / (kind + ".sh"), entry)
        initial = (project / "pyproject.toml").read_bytes()
        action_tmp = workspace / "tmp"
        action_tmp.mkdir()
        env = dict(os.environ, TMPDIR=str(action_tmp))
        if tool == "uv":
            # Exercise the supported uv fallback without the PyPA build frontend.
            bin_dir = workspace / "bin"
            bin_dir.mkdir()
            shim = bin_dir / "python3"
            shim.write_text("#!/bin/sh\nexit 1\n")
            shim.chmod(0o755)
            env["PATH"] = str(bin_dir) + ":" + env["PATH"]
        for tag in (12, 13):
            version = "2026.10.30." + str(tag)
            expected = {"gvisor-" + version + "-py3-none-any.whl", "gvisor-" + version + ".tar.gz"}
            out = workspace / str(tag)
            result = subprocess.run(["bash", str(entry), "build", str(out), "release-20261030." + str(tag)], cwd=workspace, env=env, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stdout[-1000:] + result.stderr[-1500:])
            names = {p.name for p in out.iterdir()}
            if kind == "stock" and tag == 13:
                previous = {n.replace(".13", ".12") for n in expected}
                assert names == expected | previous, names
            else:
                assert names == expected, names
            wheel = next(p for p in out.glob("*.whl") if version in p.name)
            sdist = next(p for p in out.glob("*.tar.gz") if version in p.name)
            with zipfile.ZipFile(wheel) as archive:
                md = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
                assert "Version: " + version in archive.read(md).decode().splitlines()
            with tarfile.open(sdist) as archive:
                md = next(m for m in archive.getmembers() if m.name.endswith("/PKG-INFO"))
                assert "Version: " + version in archive.extractfile(md).read().decode().splitlines()
            unchanged = (project / "pyproject.toml").read_bytes() == initial
            assert unchanged == (kind == "fixed")
            assert not any(p.is_dir() for p in action_tmp.iterdir()), list(action_tmp.iterdir())
            receipt.append(dict(arm=kind, frontend=tool, tag=version, files=sorted(names), project_unchanged=unchanged))
print(json.dumps(receipt, indent=2))
with (Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]) / "ordinary.json").open("w") as f:
    json.dump(receipt, f, indent=2)

import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

root = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]
fixtures = root / "test/rbe/python_release_isolation"
tmp = Path(os.environ["TEST_TMPDIR"]) / "member-test"
project = tmp / "sandboxexec/sandbox/python"
shutil.copytree(root / "sandboxexec/sandbox/python", project)
entry = tmp / "tools/make_python_release.sh"
entry.parent.mkdir()
staging = tmp / "tmp"
staging.mkdir()
env = dict(os.environ, TMPDIR=str(staging))

def build(arm, version):
    shutil.copyfile(fixtures / (arm + ".sh"), entry)
    out = tmp / version
    result = subprocess.run(["bash", str(entry), "build", str(out), "release-20261030." + version], cwd=tmp, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stdout[-800:] + result.stderr[-1500:])
    archive = out / ("gvisor-2026.10.30." + version + ".tar.gz")
    with tarfile.open(archive) as source:
        return ["/".join(n.split("/")[1:]) for n in source.getnames() if "/" in n]

stock = build("stock", "10")
old = set(p.name for p in (project / "dist").iterdir())
assert old == {"gvisor-2026.10.30.10-py3-none-any.whl", "gvisor-2026.10.30.10.tar.gz"}, old
manifest = project / "gvisor.egg-info/SOURCES.txt"
previous_manifest = manifest.read_text()
assert not any(n.startswith("dist/") for n in previous_manifest.splitlines()), previous_manifest
clean = build("fixed", "11")
assert not any(n.startswith("dist/") for n in clean), clean
# Determine whether an earlier manifest that listed a subsequently obsolete
# input is also carried over into the copied project by setuptools.
with manifest.open("a") as f:
    f.write("\ndist/gvisor-2026.10.30.10.tar.gz\n")
cached = build("fixed", "12")
assert cached == clean, cached
assert not any(p.is_dir() for p in staging.iterdir())
data = {"backend": "setuptools", "prior_archives": sorted(old), "old_manifest": previous_manifest.splitlines(), "stock_members": stock, "clean_members": clean, "cached_members": cached}
print(json.dumps(data, indent=2))
(Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]) / "release.json").write_text(json.dumps(data, indent=2))

# The public "all" command must resolve its relative upload directory in
# the caller, after staging's temporary working directory is gone.
cwd = tmp.parent / "all-relative"
source = cwd / "sandboxexec/sandbox/python"
shutil.copytree(root / "sandboxexec/sandbox/python", source)
entry = cwd / "tools/make_python_release.sh"
entry.parent.mkdir()
shutil.copyfile(fixtures / "fixed.sh", entry)
stage = cwd / "temporary"
stage.mkdir()
bin_dir = cwd / "home/.local/bin"
bin_dir.mkdir(parents=True)
twine = bin_dir / "twine"
twine.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@" > "$TWINE_RECORD"\n')
twine.chmod(0o755)
env = os.environ | {"TMPDIR": str(stage), "HOME": str(cwd / "home"), "TWINE_RECORD": str(cwd / "observed-upload"), "PATH": str(bin_dir) + ":" + os.environ["PATH"]}
run = subprocess.run(["bash", str(entry), "all", "relative", "relative", "release-20261030.14"], cwd=cwd, env=env, capture_output=True, text=True)
assert run.returncode == 0 and str(cwd / "relative") in run.stdout, (run.returncode, run.stdout[-1300:], run.stderr[-1000:])
observed = (cwd / "observed-upload").read_text().splitlines()
assert observed[0] == str(cwd) and observed[1] == "upload" and len(observed) == 6, observed
assert {Path(v).parent for v in observed[-2:]} == {cwd / "relative"}, observed
assert not any(stage.iterdir()), list(stage.iterdir())
# Reject both frontends after staging starts. This must remove the private copy.
for tool in ("python3", "uv"):
    exe = bin_dir / tool
    exe.write_text("#!/bin/sh\nexit 23\n")
    exe.chmod(0o755)
bad = subprocess.run(["bash", str(entry), "build", "failed", "release-20261030.15"], cwd=cwd, env=env, capture_output=True, text=True)
assert bad.returncode != 0 and not any(stage.iterdir()), (bad.returncode, list(stage.iterdir()))
(Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]) / "cwd.json").write_text(json.dumps({"all_relative": run.returncode, "upload": "local argument recorder (no network)", "failed_frontends": bad.returncode, "temporary_files_remaining": len(list(stage.iterdir()))}) + "\n")
