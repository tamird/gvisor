"""Check each file emitted by //:release against its requested target CPU."""

import json
import os
from pathlib import Path
import subprocess
import sys

from full import FILES
import qualify as q


def check_release(config: str, machine: int) -> None:
    bazel = str(q.TMP / "bazel")
    bep = q.TMP / (config + ".bep")
    result = {"source": q.git("rev-parse", "HEAD"), "target": "//:release",
              "config": config, "expected_machine": machine, "files": {}}
    receipt = q.OUT / (config + ".json")
    try:
        proc = subprocess.run([
            bazel, "build", "--config=" + config, "-c", "opt", "--jobs=4",
            "--remote_cache=", "--remote_executor=", "--disk_cache=",
            "--build_event_json_file=" + str(bep), "--color=no", "//:release",
        ])
        result["build_exit"] = proc.returncode
        result["events"] = events = q.events(bep)
        proc.check_returncode()
        assert events["owners"] == [True] and events["exit"] == 0, events
        root = Path("bazel-bin/release")
        actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        assert actual == FILES, (FILES - actual, actual - FILES)
        for name in sorted(actual):
            path = root / name
            result["files"][name] = info = q.elf(path)
            assert info["load"] and info["entry"] and os.access(path, os.X_OK), (name, info)
            assert info["machine"] == machine, (name, info)
        for name in ("gvisor-sentry-prewarmer", "gvisor-proc-umounter", "runsc-fd-parking"):
            info = result["files"]["gvisor-bin/" + name]
            assert not info["interp"] and not info["dynamic"], (name, info)
        if machine == 62:
            version = subprocess.check_output([root / "runsc", "--version"], text=True)
            assert "runsc version" in version, version
            result["version"] = version
        result["verified"] = True
    finally:
        receipt.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    config = sys.argv[1]
    check_release(config, {"x86_64": 62, "aarch64": 183}[config])
