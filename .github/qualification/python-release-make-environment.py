"""Compare the actual release job's Make plan before and after the hook merge."""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

import yaml


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def plan(source, mode, tmp):
    git("checkout", "--quiet", "--detach", source)
    steps = yaml.safe_load(Path(".buildkite/release.yaml").read_text())["steps"]
    job, = [s for s in steps if s.get("label") == ":ship: Release"]
    command, = [shlex.split(c) for c in job["commands"] if c.startswith("make ") and c.endswith("artifacts-python")]
    assert job["env"] == {"DOCKER_BUILD": "false", "PYTHON_RELEASE_DEST": "artifacts/python"}
    assert command == (["make", "DOCKER_BUILD=false", "artifacts-python"] if mode == "command line" else ["make", "artifacts-python"])
    env = dict(os.environ, **job["env"], PATH=f"{tmp}/guard:{os.environ['PATH']}")

    def make(flags):
        p = subprocess.run(["make", "--no-print-directory", flags, *command[1:]], env=env, text=True, capture_output=True, timeout=25)
        if p.returncode:
            raise ValueError(f"make {flags} returned {p.returncode}: {p.stderr[-450:]}")
        return p.stdout

    commands = make("-n")
    database = make("-np")
    assert re.search(rf"^# {mode}\nDOCKER_BUILD\s*[:?]?=\s*false$", database, re.M)
    graph = {}
    for goal in ("artifacts-python", "ensure-bazel-server"):
        rule, = re.findall(rf"^{goal}:([^\n]*)$", database.split("# Files\n", 1)[1], re.M)
        graph[goal] = rule.split()
    assert graph == {"artifacts-python": ["ensure-bazel-server"], "ensure-bazel-server": []}
    for part in ("tools/make_python_release.sh version", "build ", "cquery ", "//sandboxexec/sandbox/python:dist", "--//sandboxexec/sandbox/python:dist_version=", '/*.whl', '/*.tar.gz', '"artifacts/python"'):
        assert part in commands, part
    assert "docker" not in commands.lower()
    return commands, {"source": git("rev-parse", "HEAD"), "origin": mode, "command": command,
                      "graph": graph, "planSHA256": hashlib.sha256(commands.encode()).hexdigest()}


def main():
    tmp = Path(os.environ["RUNNER_TEMP"])
    record = {"verified": False, "started": time.time(), "arms": {}}
    try:
        (tmp / "guard").mkdir()
        for name in ("bazel", "docker", "gcloud"):
            path = tmp / "guard" / name
            path.write_text('#!/bin/sh\necho "${0##*/}" >> "$RUNNER_TEMP/make-plan-guard-calls"\nexit 94\n')
            path.chmod(0o755)
        new = git("rev-parse", "HEAD^")
        old = git("rev-parse", "HEAD^^1")
        assert old.startswith("11013b7d6") and new.startswith("aa393c5ac")
        assert git("rev-parse", "HEAD^^2").startswith("eafcb8392")
        old_plan, record["arms"]["commandLine"] = plan(old, "command line", tmp)
        new_plan, record["arms"]["environment"] = plan(new, "environment", tmp)
        assert old_plan == new_plan, "expanded release recipes differ"
        (tmp / "matched-make-plan.txt").write_text(new_plan)
        assert not (tmp / "make-plan-guard-calls").exists(), "external tool invoked"
        record["verified"] = True
    finally:
        record["finished"] = time.time()
        (tmp / "python-release-make-plan.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record))


if __name__ == "__main__":
    main()
