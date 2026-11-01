"""Run just the Buildkite producer, with all external publication disabled."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import traceback
from zipfile import ZipFile

import yaml


BASE = "db2dd58402d68b5eb47a43803a81f1b0be39935a"
FIX = "11013b7d621531ffb6ce0e33656bb487388c715c"
ORIGINAL = "8e9445224ec776e76ca11c34b28cd985679d7521197e14030b8601c5dae93238"
tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "producer.json"
receipt = {"carrier": os.environ["GITHUB_SHA"], "verified": False, "arms": {}}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    out.write_text(json.dumps(receipt, indent=2) + "\n")


def check(name, test):
    if not test:
        raise ValueError(name)


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def archives(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.iterdir() if p.is_file()} if directory.is_dir() else {}


def main():
    original = tmp / "make.zip"
    check("original_archive", hashlib.sha256(original.read_bytes()).hexdigest() == ORIGINAL)
    with ZipFile(original) as z:
        old = json.loads(z.read("results.json"))
    expected = old["baseline"]
    check("original_staged_outputs",
          old["verdict"] == "DOCKER_FREE_MAKE_STAGES_BOTH_ORIGINAL_ARCHIVES" and
          old["make"]["native_exit"] == 0 and old["make"]["files"] == expected and
          set(expected) == {"gvisor-2026.10.29.2-py3-none-any.whl", "gvisor-2026.10.29.2.tar.gz"})
    receipt.update(original_archive=ORIGINAL, original_outputs=expected)
    check("one_file_revision", git("diff", "--name-only", BASE, FIX) == ".buildkite/release.yaml")
    guard = tmp / "guard"
    guard.mkdir()
    stubs = {
        "docker": 'printf "%s\\n" "${1:-missing}" >> "$DOCKER_ATTEMPTS"\necho DOCKER_DENIED >&2\nexit 91\n',
        "date": 'if [ "$#" = 1 ] && [ "$1" = "+%Y.%m.%d" ]; then echo 2026.10.29; else exec /usr/bin/date "$@"; fi\n',
        "gcloud": 'if [ "$*" = "artifacts versions list --package=gvisor --repository=gvisor--pypi --location=us --project=oss-exit-gate-prod --format=value(version)" ]; then echo lookup >> "$VERSION_LOOKUPS"; printf "%s\\n" 2026.10.29.0 2026.10.29.1; else echo denied >> "$VERSION_LOOKUPS"; echo GCLOUD_DENIED >&2; exit 92; fi\n',
    }
    for name, body in stubs.items():
        path = guard / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o700)
    for label, source, mode in (("before", BASE, "true"), ("after", FIX, "false")):
        git("checkout", "--detach", source)
        check("source_identity", git("rev-parse", "HEAD") == source and not git("diff", "--name-only", "HEAD"))
        pipeline = yaml.safe_load(Path(".buildkite/release.yaml").read_text())
        release, = (s for s in pipeline["steps"] if s.get("label") == ":ship: Release")
        cmd, = (shlex.split(s) for s in release["commands"] if "artifacts-python" in s)
        check("real_producer_command", cmd == ["make", "DOCKER_BUILD=" + mode, "artifacts-python"])
        env = {**os.environ, **release["env"], "PATH": str(guard) + ":" + os.environ["PATH"],
               "DOCKER_CLI_PATH": str(guard / "docker"), "DOCKER_ATTEMPTS": str(tmp / (label + "-docker")),
               "VERSION_LOOKUPS": str(tmp / (label + "-ar")), "MAKEFLAGS": "STARTUP_OPTIONS=--bazelrc=" + str(tmp / "sidecar.bazelrc")}
        stage = Path(env["PYTHON_RELEASE_DEST"])
        check("job_stage", env["DOCKER_BUILD"] == "false" and stage == Path("artifacts/python") and not archives(stage))
        row = {"source": source, "command": cmd, "started": now()}
        receipt["arms"][label] = row
        save()
        proc = subprocess.run(["timeout","--kill-after=15s","15m",*cmd], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        row.update(exit=proc.returncode, finished=now())
        save()
        docker = Path(env["DOCKER_ATTEMPTS"])
        lookup = Path(env["VERSION_LOOKUPS"])
        row.update(docker_calls=len(docker.read_text().splitlines()) if docker.exists() else 0,
                   registry_reads=len(lookup.read_text().splitlines()) if lookup.exists() else 0,
                   explicit_denial="DOCKER_DENIED" in proc.stdout,
                   invocations=re.findall(r"Invocation ID: ([a-f0-9-]{36})", proc.stdout),
                   files=archives(stage))
        row["errors"] = [s for s in ("DOCKER_DENIED","GCLOUD_DENIED","malformed expression",
                                      "no such target","Build did NOT complete successfully","Permission denied",
                                      "Error downloading","ERROR: An error occurred during the fetch")
                         if s in proc.stdout]
        save()
        check("tracked_tree", not git("diff", "--name-only", "HEAD"))
        if label == "before":
            row["verified"] = (row["exit"] == 2 and row["docker_calls"] > 0 and
                               row["explicit_denial"] and not row["files"])
            save()
            continue
        check("native_staging", row["exit"] == 0 and row["docker_calls"] == 0 and
              row["registry_reads"] == 1 and lookup.read_text() == "lookup\n" and row["files"] == expected and len(row["invocations"]) >= 2)
        check("actual_bazel_outputs", archives(Path("bazel-bin/sandboxexec/sandbox/python/dist")) == expected)
        # Preview the existing signer's argv. No key, native packages, or signer is used.
        handoff = subprocess.run(["make","-n","release","RELEASE_ARTIFACTS=prior-native","RELEASE_KEY=/dev/null"],
                                 env=env, capture_output=True, text=True)
        row["handoff_dry_run_exit"] = handoff.returncode
        save()
        line, = [l for l in handoff.stdout.replace("\\\n", " ").splitlines() if "tools/make_release.sh" in l]
        args = shlex.split(line)
        i = args.index("tools/make_release.sh")
        staged = args[i+3:]
        native = [f"prior-native/{a}/{f}" for a in ("x86_64","aarch64")
                  for f in ("runsc.deb","gvisor.tar.bz2","gvisor.tar.zstd")]
        check("signer_dry_run_inputs", handoff.returncode == 0 and
              staged == [*native, *(str(stage / f) for f in expected)])
        row["handoff_paths"] = staged
    check("original_requires_builder", receipt["arms"]["before"]["verified"])
    receipt.update(verified=True, verified_at=now())


try:
    main()
except Exception as exc:
    receipt["exception"] = type(exc).__name__
    if isinstance(exc, ValueError) and re.fullmatch(r"[a-z_]{1,80}", str(exc)):
        receipt["failed_check"] = str(exc)
    receipt["line"] = traceback.extract_tb(exc.__traceback__)[-1].lineno
finally:
    save()
sys.exit(0 if receipt["verified"] else 1)
