"""Existing release qualification helpers: typed events, ELF identity and failures."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess

from image_inventory import PIN

TMP = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
OUT = TMP / "public"
PRODUCT = "87fd1b411e0f2d06557f04e1f33b154692cfd2eb"
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


def failure_tokens(data):
    """Expose only fixed tool names and failure classes, never command text."""
    text = data.decode(errors="replace").lower()
    tokens = ("/bin/bash", "/bin/sh", "/usr/bin/env", "binary_wrapper.sh",
              "genrule-setup.sh", "modulemap", "python3", "python", "clang++",
              "link-wrapper-clang++", "ld.lld", "libc.so.6", "libstdc++.so.6",
              "/lib64/ld-linux-x86-64.so.2", "/lib/ld-linux-aarch64.so.1",
              "no such file", "not found", "failed to start", "exec format error",
              "permission denied", "undefined symbol", "unable to find library",
              "no usable strategy", "no matching toolchains", "unauthenticated",
              "resource_exhausted", "deadline_exceeded", "timed out")
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "tokens": [token for token in tokens if token in text]}


def stream_identity(file):
    match = re.fullmatch(r"bytestream://remote[.]buildbuddy[.]io/(?:[^?]+/)?blobs/([a-f0-9]{64})/([0-9]+)", file.get("uri", ""))
    if match:
        return {"hash": match[1], "sizeBytes": int(match[2])}
    return {"digest": file.get("digest"), "length": file.get("length")}


def elf(path):
    raw = path.read_bytes()
    assert raw[:6] == b"\x7fELF\x02\x01" and len(raw) > 64
    base = struct.unpack_from("<Q", raw, 32)[0]
    size, count = struct.unpack_from("<HH", raw, 54)
    types = [struct.unpack_from("<I", raw, base + i * size)[0] for i in range(count)]
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "machine": struct.unpack_from("<H", raw, 18)[0], "entry": struct.unpack_from("<Q", raw, 24)[0],
            "load": 1 in types, "interp": 3 in types, "dynamic": 2 in types}


def stub_elf(raw):
    assert raw[:6] == b"\x7fELF\x02\x01"
    phoff = struct.unpack_from("<Q", raw, 32)[0]
    phsize, phcount = struct.unpack_from("<HH", raw, 54)
    types = [struct.unpack_from("<I", raw, phoff + i * phsize)[0] for i in range(phcount)]
    shoff = struct.unpack_from("<Q", raw, 40)[0]
    shsize, shcount, shstr = struct.unpack_from("<HHH", raw, 58)
    sections = [struct.unpack_from("<IIQQQQIIQQ", raw, shoff + i * shsize) for i in range(shcount)]
    names = sections[shstr]
    strings = raw[names[4]:names[4] + names[5]]
    def cstring(data, offset):
        return data[offset:].split(b"\0", 1)[0].decode()
    by_name = {cstring(strings, s[0]): s for s in sections}
    result = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "machine": struct.unpack_from("<H", raw, 18)[0], "type": struct.unpack_from("<H", raw, 16)[0], "interpreter": 3 in types}
    exports = {}
    for s in sections:
        if s[1] != 2:
            continue
        strs = sections[s[6]]
        table = raw[strs[4]:strs[4] + strs[5]]
        for off in range(s[4], s[4] + s[5], s[9]):
            name, info, other, section, value, size = struct.unpack_from("<IBBHQQ", raw, off)
            name = cstring(table, name)
            if section and name.startswith("__export_"):
                exports[name.replace(".", "_")] = value
    blob = None
    if ".crblob" in by_name:
        s = by_name[".crblob"]
        assert s[3] == 0 and s[2] & 2
        assert not any(x != s and x[2] & 2 and x[5] for x in sections)
        blob = raw[s[4]:s[4] + s[5]]
    return result, blob, exports
