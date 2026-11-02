"""Use the existing native OCI checkpoint lifecycle on a fixed matched cohort."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import traceback

ROOT = Path(os.environ["RUNNER_TEMP"])
OUT = ROOT / "proc-cache-results"
BASE = "0cf7325e46a196a3c9c8be8c7c4f8a210700a07c"
CURRENT = "918aefd5f3771a161013fb72873d8aa19196e1bc"
ID = "proc-cache"
REPORT = {"status": "INCOMPLETE", "sources": {"base": BASE, "current": CURRENT}, "trials": [],
          "rounds": 6, "excluded_round": -1, "platform": "systrap", "compression": "none", "engine": "graph"}


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def save():
    (OUT / "comparison.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def runsc(install, bundle, *args, required=True):
    cmd = [str(install / "runsc"), "--root=" + str(bundle / "runtime"), "--platform=systrap",
           "--network=none", "--overlay2=none", "--host-settings=ignore", "--ignore-cgroups", *args]
    result = subprocess.run(cmd, env={**os.environ, "GVISOR_SIDECAR_BINARIES_DIR": str(install / "gvisor-bin")},
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    if required and result.returncode:
        # runsc and this guest receive no credential environment in this phase.
        error = (result.stderr or result.stdout).splitlines()
        raise RuntimeError((args[0], result.returncode, error[-8:]))
    return result.stdout


def trial(bundle, number, arm, mode):
    row = {"arm": arm, "round": number, "mode": mode, "status": "INCOMPLETE"}
    REPORT["trials"].append(row)
    save()
    install = ROOT / ("install-" + arm)
    fs = bundle / "rootfs"
    fs.mkdir()
    for name in ("usr", "dev", "sys", "proc", "work", "etc"):
        (fs / name).mkdir()
    for link in ("bin", "lib", "lib64", "sbin"):
        (fs / link).symlink_to("usr/" + link)
    shutil.copyfile(ROOT / "proc-cache"/"guest.py", fs / "guest.py")
    image = bundle / "image"
    image.mkdir()
    (bundle / "runtime").mkdir()
    spec = dict(ociVersion="1.0.2", root=dict(path="rootfs", readonly=True),
                process=dict(terminal=False, user=dict(uid=0, gid=0), cwd="/",
                             args=["/usr/bin/python3", "-B", "/guest.py", "init"],
                             env=["PATH=/usr/bin:/bin", "LC_ALL=C", "PYTHONDONTWRITEBYTECODE=1"]),
                mounts=[dict(destination="/usr", type="bind", source="/usr", options=["rbind", "ro"]),
                        dict(destination="/proc", type="proc", source="proc"),
                        dict(destination="/sys", type="sysfs", source="sysfs"),
                        dict(destination="/work", type="tmpfs", source="tmpfs")],
                linux=dict(namespaces=[dict(type=t) for t in ("pid", "mount", "ipc", "uts", "network")]))
    (bundle / "config.json").write_text(json.dumps(spec))

    def cli(*args):
        return runsc(install, bundle, *args)

    def sample(phase):
        return json.loads(cli("exec", ID, "/usr/bin/python3", "-B", "/guest.py", phase))

    try:
        row["phase"] = "run"
        cli("run", "--detach", "--bundle=" + str(bundle), ID)
        row["phase"] = "prepare"
        before = sample(mode)
        row["before"] = before
        row["phase"] = "checkpoint"
        save()
        start = time.perf_counter_ns()
        cli("checkpoint", "--state-engine=graph", "--compression=none", "--image-path=" + str(image), ID)
        row["checkpoint_ns"] = time.perf_counter_ns() - start
        row["files"] = {name: (image / name).stat().st_size for name in ("checkpoint.img", "pages_meta.img", "pages.img")}
        row["checkpoint_sha256"] = sha(image / "checkpoint.img")
        assert min(row["files"].values()) > 0
        cli("delete", "--force", ID)
        row["phase"] = "restore"
        save()
        start = time.perf_counter_ns()
        cli("restore", "--detach", "--bundle=" + str(bundle), "--image-path=" + str(image), ID)
        row["restore_ns"] = time.perf_counter_ns() - start
        row["phase"] = "first-use"
        after = sample("restored")
        row["restore_through_full_probe_ns"] = time.perf_counter_ns() - start
        row["after"] = after
        assert before["meta"] == after["meta"]
        assert len(set(after["stat_inos"])) == 16
        for obs in [after] + ([before] if mode == "primed" else []):
            assert len(obs["ps_ns"]) == len(obs["cpu"]) == 2 and min(obs["ps_ns"]) > 0
            assert all(c["ns"] > 0 and (c["online"], c["masks"]) == (after["cpu"][0]["online"], after["cpu"][0]["masks"]) for c in obs["cpu"])
        if mode == "primed":
            row["stat_inodes_retained"] = sum(a == b for a, b in zip(before["stat_inos"], after["stat_inos"], strict=True))
        row["status"] = "PASS"
        del row["phase"]
        save()
    finally:
        runsc(install, bundle, "delete", "--force", ID, required=False)


def main():
    OUT.mkdir(exist_ok=True)
    REPORT["host_tool_sha256"] = {p: sha(Path(p)) for p in ("/usr/bin/ps", "/usr/bin/python3", "/usr/bin/sleep")}
    REPORT["guest_sha256"] = sha(ROOT / "proc-cache" / "guest.py")
    for arm, source in REPORT["sources"].items():
        b = json.loads((OUT / (arm + "-build.json")).read_text())
        assert b["source"] == source and b["exit"] == 0 and b["result"] == "PASS" and "//:release" in b["completed"]
        assert b["outputs"]["runsc"] == sha(ROOT / ("install-" + arm) / "runsc")
        assert all(b["outputs"][path] == sha(ROOT / ("install-" + arm) / path) for path in b["outputs"])
    assert json.loads((OUT/"base-build.json").read_text())["outputs"]["gvisor-bin/gvisor_sentry"] != json.loads((OUT/"current-build.json").read_text())["outputs"]["gvisor-bin/gvisor_sentry"]
    for number in range(-1, REPORT["rounds"]):
        for mode in (("quiet", "primed") if number % 2 == 0 else ("primed", "quiet")):
            for arm in (("base", "current") if number % 2 == 0 else ("current", "base")):
                with tempfile.TemporaryDirectory(dir=ROOT) as directory:
                    trial(Path(directory), number, arm, mode)
    assert len(REPORT["trials"]) == 28 and all(row["status"] == "PASS" for row in REPORT["trials"])
    REPORT["status"] = "PASS"


try:
    main()
except Exception as exc:
    REPORT["error"] = str(exc)[-1400:]
    REPORT["line"] = traceback.extract_tb(exc.__traceback__)[-1].lineno
    raise
finally:
    REPORT["finished_ns"] = time.time_ns()
    save()
