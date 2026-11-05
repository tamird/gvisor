"""Existing release qualification helpers: typed events, ELF identity and failures."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess

from image_inventory import PIN

TMP = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
OUT = TMP / "public"
PRODUCT = "c43bcb023757ee2a2d31e656ac495a27cb29e3be"
PUBLIC = "//:release"
PROOF = ["--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(TMP / "private.bazelrc")]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(name, row):
    (OUT / (name + ".json")).write_text(json.dumps(row, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def events(path):
    receipt = {"owners": [], "test_results": 0}
    if path.exists():
        for line in path.open():
            event = json.loads(line)
            if "started" in event:
                receipt.update(invocation=event["started"]["uuid"], bazel=event["started"].get("buildToolVersion"))
            if "finished" in event:
                receipt.update(finished=event["finished"]["finishTime"], exit=event["finished"]["exitCode"].get("code", 0))
            receipt["test_results"] += int("testResult" in event.get("id", {}))
            target = event["id"].get("targetCompleted", {})
            if target.get("label") == PUBLIC and not target.get("aspect"):
                receipt["owners"].append(event.get("completed", {}).get("success", False))
    return receipt


def diagnostics(path):
    if not path.is_file():
        return []
    rows = []
    reasons = ("undefined:", "not found", "undeclared", "no such file", "permission denied",
               "no matching toolchains", "does not have mandatory providers", "no such target",
               "no such package", "compilation failed", "build did not complete", "error:",
               "not defined", "missing strict dependencies", "has no field or method", "timeout",
               "unauthenticated", "resource_exhausted", "failed to fetch", "command not found")
    for line in path.read_text(errors="replace").splitlines():
        kinds = [word for word in reasons if word in line.lower()]
        if not kinds:
            continue
        item = {"kinds": kinds}
        if item not in rows:
            rows.append(item)
    return rows[:18]


def elf(path):
    return elf_bytes(path.read_bytes())


def elf_bytes(raw):
    assert raw[:6] == b"\x7fELF\x02\x01" and len(raw) > 64
    base = struct.unpack_from("<Q", raw, 32)[0]
    size, count = struct.unpack_from("<HH", raw, 54)
    types = [struct.unpack_from("<I", raw, base + i * size)[0] for i in range(count)]
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "machine": struct.unpack_from("<H", raw, 18)[0], "entry": struct.unpack_from("<Q", raw, 24)[0],
            "load": 1 in types, "interp": 3 in types, "dynamic": 2 in types}
