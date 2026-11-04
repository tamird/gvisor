"""Exercise the release Make targets in each source tree without publishing."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile

FILES = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "test/rbe/python_release_isolation"
WORK = Path(os.environ["TEST_TMPDIR"])
OUT = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]) / "verdict.json"
DATE = "20261030"
TAG = "release-" + DATE
REPORT = {"status": "INCOMPLETE", "cases": {}}


def save():
    OUT.write_text(json.dumps(REPORT, indent=2) + "\n")


def script(path, content):
    path.write_text("#!/bin/sh\nset -eu\n" + content + "\n")
    path.chmod(0o755)


def command(args, cwd, env, *, check=True):
    run = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and run.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} exited {run.returncode}: {run.stdout[-1500:]}")
    return run


def names(version):
    return {f"gvisor-{version}-py3-none-any.whl", f"gvisor-{version}.tar.gz"}


def version(revision):
    return f"2026.10.30.{revision}"


def archive_version(path):
    if path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as archive:
            member, = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
            lines = archive.read(member).decode().splitlines()
    else:
        with tarfile.open(path) as archive:
            member, = [m for m in archive.getmembers() if "/" in m.name and m.name.count("/") == 1 and m.name.endswith("/PKG-INFO")]
            lines = archive.extractfile(member).read().decode().splitlines()
    value, = [line.removeprefix("Version: ") for line in lines if line.startswith("Version: ")]
    return value


def archives(directory):
    return {p.name: archive_version(p) for p in directory.iterdir()
            if p.name.startswith("gvisor-") and (p.name.endswith(".whl") or p.name.endswith(".tar.gz"))}


def prepare(arm, frontend):
    work = WORK / (arm + "-" + frontend)
    work.mkdir()
    # Both inputs are git archives of the exact source revision.
    with tarfile.open(FILES / (arm + ".tar")) as archive:
        archive.extractall(work, filter="data")
    script_path = work / "tools/make_python_release.sh"
    frontend_dir = work / "bin"
    frontend_dir.mkdir()
    home = work / "home"
    publish_tools = home / ".local/bin"
    publish_tools.mkdir(parents=True)
    twine_record = work / "twine-record"
    script(publish_tools / "twine", 'printf "%s\\n" "$@" >> "$TWINE_RECORD"')
    # The test owns no Debian payload. Keep the release Makefile, release
    # script, checksum writer and manifest generator exact; isolate apt signing.
    script(work / "tools/make_apt.sh", 'printf "%s\\n" "$2" >> "$APT_RECORD"')
    env = os.environ | {"HOME": str(home), "PATH": str(frontend_dir) + ":" + os.environ["PATH"],
                         "TWINE_RECORD": str(twine_record), "APT_RECORD": str(work / "apt-record"),
                         "PYTHON_FOR_FIXTURE": sys.executable, "DRY_RUN": "1"}
    if frontend == "uv":
        script(frontend_dir / "python3", 'if [ "${1:-} ${2:-}" = "-m build" ]; then exit 23; fi\nexec "$PYTHON_FOR_FIXTURE" "$@"')
    for argv in (["git", "init", "-q"], ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "Fixture"],
                 ["git", "tag", TAG + ".2"], ["git", "config", f"url.{work}.insteadOf", "https://github.com/google/gvisor.git"]):
        command(argv, work, env)
    # Satisfy unrelated make release prerequisites without building, importing
    # or signing any OS binaries. The ordinary Make targets stage real Python archives.
    (work / "artifacts/x86_64").mkdir(parents=True)
    (work / "fixture.key").touch()
    return work, env, script_path


def make_comparison(arm, frontend):
    work, env, entry = prepare(arm, frontend)
    case = REPORT["cases"][arm + "-" + frontend] = {}
    save()
    project = work / "sandboxexec/sandbox/python/pyproject.toml"
    initial = project.read_bytes()
    for revision in (1, 2):
        built = command(["make", "DOCKER_BUILD=false", "artifacts-python", f"RELEASE_NAME={TAG}.{revision}"], work, env)
        expected_frontend = "building with uv build..." if frontend == "uv" else "with python3 -m build."
        assert expected_frontend in built.stdout, built.stdout[-900:]
    staging = archives(work / "artifacts/python")
    expected = names(version(2))
    if arm == "stock":
        expected |= names(version(1))
    assert set(staging) == expected, staging
    assert all(name in names(value) for name, value in staging.items())
    assert project.read_bytes() == initial
    command(["make", "DOCKER_BUILD=false", "RELEASE_KEY=fixture.key", "release"], work, env)
    for destination in (DATE, DATE + ".2"):
        raw = work / "repo/release" / destination / "python"
        assert archives(raw) == staging
        for archive in staging:
            digest, filename = (raw / (archive + ".sha512")).read_text().split()
            assert digest == hashlib.sha512((raw / archive).read_bytes()).hexdigest() and filename == archive
    calls = (work / "twine-record").read_text().splitlines()
    assert calls[:3] == ["upload", "--repository-url", "https://us-python.pkg.dev/oss-exit-gate-prod/gvisor--pypi"]
    assert {Path(p).name for p in calls[3:]} == expected
    assert not (work / "repo/release/latest/python").exists()
    manifest = command(["bash", str(entry), "publish-manifest", "repo/release"], work, env, check=False)
    if arm == "stock":
        assert manifest.returncode == 1 and "expected one wheel version" in manifest.stdout
    else:
        assert manifest.returncode == 0 and f'"version": "{version(2)}"' in manifest.stdout
        assert "DRY_RUN is set; skipping upload." in manifest.stdout
    case.update(status="PASS", frontend=frontend, archives=staging, tag_directories=[DATE, DATE + ".2"],
                upload=sorted(expected), manifest_exit=manifest.returncode, real_upload=False)
    save()
    return work, env, entry


def shared_destination(work, env, entry):
    dest = work / "shared destination"
    dest.mkdir()
    sentinels = ("gvisor_tools-9.0-py3-none-any.whl", "gvisor-tools-9.0.tar.gz",
                 "another-1-py3-none-any.whl", "another-1.tar.gz", "notes.txt")
    for name in sentinels:
        (dest / name).write_bytes(name.encode())
    def run(revision, extra=None, **kwargs):
        return command(["bash", str(entry), "build", str(dest), f"{TAG}.{revision}"], work, env | (extra or {}), **kwargs)
    run(1)
    run(2)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.iterdir()}
    assert set(before) == set(sentinels) | names(version(2)), before
    reject = work / "reject"
    reject.mkdir()
    for tool in ("python3", "uv"):
        script(reject / tool, "exit 23")
    bad = run(3, {"PATH": str(reject) + ":" + env["PATH"]}, check=False)
    assert bad.returncode != 0 and before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.iterdir()}
    assert all((dest / n).read_bytes() == n.encode() for n in sentinels)
    # Put two real frontend builds at the staging boundary together. Both
    # must wait for the same directory, then leave exactly one complete pair.
    real_flock = shutil.which("flock", path=env["PATH"])
    assert real_flock
    script(work / "bin/flock", 'touch "$ARRIVED"\nexec "$REAL_FLOCK" "$@"')
    arrived = [work / ("writer-" + str(n)) for n in (3, 4)]
    descriptor = os.open(dest, os.O_RDONLY)
    writers = []
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        for revision, marker in zip((3, 4), arrived):
            writers.append(subprocess.Popen(["bash", str(entry), "build", str(dest), f"{TAG}.{revision}"], cwd=work,
                           env=env | {"ARRIVED": str(marker), "REAL_FLOCK": real_flock},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        deadline = time.monotonic() + 80
        while not all(p.exists() for p in arrived):
            if any(p.poll() is not None for p in writers) or time.monotonic() >= deadline:
                raise RuntimeError("Both builds did not reach destination staging")
            time.sleep(.05)
        assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.iterdir()}
    finally:
        os.close(descriptor)
    assert [p.wait(timeout=60) for p in writers] == [0, 0]
    remaining = {p.name for p in dest.iterdir()} - set(sentinels)
    assert remaining in (names(version(3)), names(version(4))), remaining
    assert len({archive_version(dest / n) for n in remaining}) == 1
    assert all((dest / n).read_bytes() == n.encode() for n in sentinels)
    REPORT["shared_destination"] = {"status": "PASS", "unrelated_preserved": list(sentinels),
         "frontend_failure_exit": bad.returncode, "previous_files_identical_after_failure": True,
         "writers_at_the_same_lock": 2, "surviving_files": sorted(remaining)}


try:
    save()
    package, release, essential = os.environ["RELEASE_FLOCK_PACKAGE"].split()
    assert package == "util-linux" and essential == "yes"
    REPORT["release_worker"] = {"image": "sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a",
                                "util_linux": release, "essential": True, "directory_lock": "PASS"}
    for frontend in ("build", "uv"):
        for arm in ("stock", "fixed"):
            fixture = make_comparison(arm, frontend)
            if (arm, frontend) == ("fixed", "build"):
                shared_destination(*fixture)
    REPORT["status"] = "PASS"
except Exception as error:
    REPORT.update(status="FAIL", error=type(error).__name__, detail=str(error)[-1700:])
    raise
finally:
    save()
