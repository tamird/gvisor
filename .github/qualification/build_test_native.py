"""Run the hosted dependency contract and the real empty-worker native tests."""
import base64
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import urllib.error
import urllib.request

import build_test as q


def hosted():
    results = {form: [q.control(form, suite, host_strategy="processwrapper-sandbox") for suite in (False, True)]
               for form in ("defaults", "barrier")}
    q.report["host_contracts"] = results
    q.report["host_qualified"] = next((f for f, result in results.items() if all(result)), None)
    # A host observation failure still allows the independently checked remote
    # hard-barrier candidate; record its status distinctly.
    q.report["choice"] = q.report["host_qualified"] or "barrier"
    q.save()
    q.main("select")


def original():
    ids = ["6ecea088-f5bc-40a2-aebd-3c2bb3a8fc1b", "c2147b66-cb52-44f0-81fc-280274d0c3bd"]
    key = os.environ.pop("BUILDBUDDY_API_KEY")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    for iid in ids:
        row = {"invocation": iid, "chunks": []}
        try:
            chunks = []
            cursor = ""
            for _ in range(4):
                data = {"invocationId": iid, "minLines": 1000}
                if cursor:
                    if not re.fullmatch("[a-zA-Z0-9_-]{1,32}", cursor):
                        break
                    data["chunkId"] = cursor
                req = urllib.request.Request("https://remote.buildbuddy.io/rpc/BuildBuddyService/GetEventLogChunk",
                    data=json.dumps(data).encode(), headers={"Content-Type": "application/json", "x-buildbuddy-api-key": key})
                with opener.open(req, timeout=25) as response:
                    body = response.read(4000001)
                if len(body) > 4000000:
                    raise ValueError("chunk exceeds limit")
                message = json.loads(body)
                piece = base64.b64decode(message.get("buffer", ""), validate=True)
                row["chunks"].append([len(piece), q.sha(piece)])
                chunks.insert(0, piece)
                raw = re.sub(rb"\x1b\[[0-9;]*[A-Za-z]", b"", b"".join(chunks)).replace(b"\r", b"\n")
                # The original invocation had exactly three failed CppLinks.
                # Expose at most one original linker message for each action.
                observations = q.link_lines(raw)[:3]
                for observation in observations:
                    observation["other_messages_sha256"] = q.sha(json.dumps(observation["messages"][1:]).encode())
                    observation["messages"] = observation["messages"][:1]
                if key and key.encode() in json.dumps(observations).encode():
                    raise ValueError("unexpected diagnostic")
                row["link_diagnostics"] = observations
                cursor = message.get("previousChunkId", "")
                if sum(bool(x["messages"]) for x in observations) >= 3 or not cursor:
                    break
        except (ValueError, OSError, KeyError) as exc:
            row["reader_status"] = getattr(exc, "code", type(exc).__name__)
        q.report.setdefault("original_diagnostic", []).append(row)
        q.save()


def wrapper(binary):
    if not binary.is_file() or binary.stat().st_size > 32000000:
        return None
    b = binary.read_bytes()
    if b[:6] != b"\x7fELF\x02\x01":
        return {"kind": "other", "sha256": q.sha(b)}
    ph, size, num = struct.unpack_from("<Q", b, 32)[0], *struct.unpack_from("<HH", b, 54)
    interpreters = []
    for i in range(num):
        where = ph + i * size
        if struct.unpack_from("<I", b, where)[0] == 3:
            start = struct.unpack_from("<Q", b, where + 8)[0]
            length = struct.unpack_from("<Q", b, where + 32)[0]
            name = b[start:start + min(256, length)].rstrip(b"\0").decode()
            interpreters.append(q.clean(name))
    return {"sha256": q.sha(b), "bytes": len(b), "machine": struct.unpack_from("<H", b, 18)[0], "PT_INTERP": interpreters}


def native(arch):
    form = q.report["choice"]
    names = [q.PKG + form + "_" + state for state in ("valid", "broken")]
    args = [q.ROOT, q.LAUNCH, *names, *(t + "_suite" for t in names)]
    row = q.run("native-" + arch, arch, True, True, "test", args)
    cp = subprocess.run(q.startup(True, False) + ["info", "execution_root"], capture_output=True)
    if cp.returncode == 0:
        root = Path(cp.stdout.decode().strip()).resolve()
        row["test_wrappers"] = {}
        for spawn in row.get("spawns", []):
            exe = spawn.get("exe") or ""
            if spawn["kind"] == "TestRunner" and Path(exe).name in ("posix_tw", "compiled_posix_test_wrapper") and not Path(exe).is_absolute() and ".." not in Path(exe).parts:
                row["test_wrappers"][exe] = wrapper(root / exe)
    row["contracts"] = {}
    good, bad = names
    for route in ("", "_via_suite"):
        def observed_input(label, code):
            return (any(s["owner"] == label + "_input" and s["kind"] == "BuildTestFixture" and s["exit"] == code
                        and s["runner"] == "remote" and not s["cache"] and s["cpu"] == arch and s["image"]
                        for s in row.get("spawns", [])) and any(
                            a["owner"] == label + "_input" and a["kind"] == "BuildTestFixture" and a["success"] == (code == 0)
                            for a in row.get("actions", [])))
        row["contracts"]["suite" if route else "direct"] = (q.passed(row, good + route, arch, True)
             and observed_input(good + route, 0) and observed_input(bad + route, 41)
             and not any(s["owner"] == bad + route and s["kind"] == "TestRunner" for s in row.get("spawns", []))
             and not any(t["owner"] == bad + route and t["status"] == "PASSED" for t in row.get("tests", [])))
    obs = row.get("launcher", {})
    wrappers = [s for s in row.get("spawns", []) if s["kind"] == "TestRunner" and s["owner"] == q.ROOT]
    row["root_wrapper"] = (len(wrappers) == 1 and Path(wrappers[0]["exe"] or "").name ==
                           {"amd64": "posix_tw", "arm64": "compiled_posix_test_wrapper"}[arch]
                           and Path(wrappers[0]["test_program"] or "").name == "build_test")
    row["root_pass"] = (row["root_wrapper"] and q.passed(row, q.ROOT, arch, True) and row.get("targets", {}).get(q.ROOT)
                        and q.fresh(row, q.LAUNCH, "InspectBuildTestLauncher", arch)
                        and all(obs.get(k) is True for k in ("/usr/bin/env_absent", "/bin/sh_absent", "/bin/bash_absent", "/bin/touch_absent", "/usr/bin/touch_absent"))
                        and obs.get("arch") == arch and obs.get("machine") == {"amd64": 62, "arm64": 183}[arch] and obs.get("exit") == 0)
    if form == "barrier":
        row["root_pass"] = row["root_pass"] and q.fresh(row, q.ROOT, "BuildTestBarrier", arch)
    row["verified"] = (row["exit"] == 1 and row["root_pass"] and all(row["contracts"].values())
                       and q.report["cells"]["graph-" + arch]["verified"])
    q.save()


if sys.argv[1] == "hosted":
    hosted()
elif sys.argv[1] == "original":
    original()
else:
    for arch in q.ARCH:
        native(arch)
    q.report["verified"] = bool(q.report["host_qualified"]) and all(q.report["cells"]["native-" + a]["verified"] for a in q.ARCH)
    q.save()
    if not q.report["verified"]:
        raise SystemExit("See original per-target outcomes")
