"""Compare the repository Make entry points with Docker unavailable."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess

BASE = "183a39d1b2aff9e8e2219171940001749a2b5db0"
PRODUCT = "f5b1fa005b08e1b7f9c61f1cafd13417b9707d1e"
GOALS = ["list-all-images", "list-all-test-images", "list-cpu-images", "list-gpu-images"]


def variants(goal):
    return [(1,1), (1,2), (2,2)] if goal in GOALS[2:] else [(1,1)]


def git(*args):
    return subprocess.check_output(["git", *args]).decode().strip()


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    work = Path(os.environ["RUNNER_TEMP"]) / "host-image-inventory"
    work.mkdir()
    docker = work / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$INVENTORY_DOCKER_CALLS"\nexit 77\n')
    docker.chmod(0o755)
    calls = work / "docker-verbs"
    env = dict(os.environ, PATH=str(work) + ":" + os.environ["PATH"], INVENTORY_DOCKER_CALLS=str(calls))
    verdict = {"base": BASE, "product": PRODUCT, "machine": platform.machine(), "cases": {}, "status": "INCOMPLETE"}
    expected = [f"{kind}/{arch}/{goal}/{part}-{total}" for kind in ("stock-explicit", "fixed-default")
                for arch in ("x86_64", "aarch64") for goal in GOALS for part,total in variants(goal)]
    expected += ["stock-default", "fixed-list-and-testlogs", "fixed-mixed-dry-run"]
    verdict["expected_cases"] = expected
    verdict["failed_cases"] = []
    baseline = {}
    current = ""

    def observe(name, args, dry=False):
        nonlocal current
        current = name
        calls.write_text("")
        row = {"execution_started_at": now()}
        verdict["cases"][name] = row
        try:
            proc = subprocess.run(["make", "-s", "--no-print-directory", *(["-n"] if dry else []), *args],
                                  capture_output=True, env=env, timeout=60)
        finally:
            row["execution_finished_at"] = now()
        verbs = calls.read_text().splitlines()
        known = {"version","image","inspect","exec","build","buildx","run","pull","tag","rm","commit","ps","info","create","load","manifest"}
        row.update({"exit": proc.returncode, "docker_calls": len(verbs), "docker_verbs": dict(Counter(v if v in known else "other" for v in verbs)),
               "stdout_sha256": hashlib.sha256(proc.stdout).hexdigest(), "stderr_sha256": hashlib.sha256(proc.stderr).hexdigest(),
               })
        if not dry and proc.returncode == 0:
            row["lines"] = len(proc.stdout.splitlines())
        return proc, verbs

    try:
        assert git("rev-parse", "HEAD^", "HEAD^^").splitlines() == [PRODUCT, BASE]
        assert git("diff", "--name-only", BASE, PRODUCT) == "tools/bazel.mk"
        assert git("rev-parse", PRODUCT + ":tools/images.mk") == git("rev-parse", BASE + ":tools/images.mk")
        for rev in (BASE, PRODUCT):
            git("checkout", "--quiet", "--detach", rev)
            assert git("rev-parse", "HEAD") == rev
            for arch in ("x86_64", "aarch64"):
                for goal in GOALS:
                    for part,total in variants(goal):
                        case = f"{arch}/{goal}/{part}-{total}"
                        opts = ["ARCH="+arch, f"PARTITION={part}", f"TOTAL_PARTITIONS={total}", goal]
                        args = ["DOCKER_BUILD=false", *opts] if rev == BASE else opts
                        name = ("stock-explicit/" if rev == BASE else "fixed-default/") + case
                        proc, verbs = observe(name, args)
                        assert proc.returncode == 0 and not verbs, name
                        names = proc.stdout.decode().splitlines()
                        assert names and all(re.fullmatch("[a-zA-Z0-9_.-]+", n) for n in names), name
                        if rev == BASE:
                            baseline[case] = proc.stdout
                        else:
                            assert proc.stdout == baseline[case], name
                    if goal in GOALS[2:]:
                        all_names, first, second = (Counter(baseline[f"{arch}/{goal}/{p}"].splitlines()) for p in ("1-1","1-2","2-2"))
                        assert first+second == all_names, (arch,goal)
            if rev == BASE:
                proc, verbs = observe("stock-default", ["ARCH=x86_64",GOALS[0]])
                assert "exec" in verbs and "version" in verbs, "stock Docker selection"
            else:
                proc, verbs = observe("fixed-list-and-testlogs", ["ARCH=x86_64",GOALS[0],"testlogs"])
                assert proc.returncode == 0 and not verbs and proc.stdout == baseline["x86_64/list-all-images/1-1"]
                _, verbs = observe("fixed-mixed-dry-run", [GOALS[0],"build","TARGETS=//runsc"], dry=True)
                assert "exec" in verbs and "version" in verbs, "mixed Docker selection"
        current = ""
        assert b"benchmarks_syscallbench" in baseline["x86_64/list-all-images/1-1"].splitlines()
        assert b"benchmarks_syscallbench" not in baseline["aarch64/list-all-images/1-1"].splitlines()
        assert set(verdict["cases"]) == set(expected)
        verdict["status"] = "PASS"
    except Exception as exc:
        verdict["failed_cases"] = [current] if current else []
        verdict["status"] = "FAIL"
        verdict["failure"] = type(exc).__name__
        verdict["location"] = []
        tb = exc.__traceback__
        while tb:
            if tb.tb_frame.f_code.co_filename == __file__:
                verdict["location"].append(tb.tb_lineno)
            tb = tb.tb_next
        raise
    finally:
        verdict["executed_cases"] = list(verdict["cases"])
        verdict["missing_cases"] = [name for name in expected if name not in verdict["cases"]]
        verdict["execution_finished_at"] = max((case["execution_finished_at"] for case in verdict["cases"].values()), default=None)
        verdict["verified_at"] = now()
        (work/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")


if __name__ == "__main__":
    main()
