"""Read only the four original consumer results and two failed Go compiles."""

import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from zipfile import ZipFile

from cas_inputs import TMP, blob, fetch, rpc

ARCHIVE = "b924c06570792e9732e95a43a786eb9933f8b6d862edcda4fce74466628c11e7"
SPLIT = "03763b0448f9e15a2247ba0a50e8b710cf4ba0e9"
UUID = "d1e538c4-070c-4200-bdb8-b3317f74d1c8"
PREFIX = "@@gazelle++go_deps+com_github_containerd_cgroups_v3//"
OUT = {"verdict": "INCOMPLETE", "archive": ARCHIVE, "observations": {}}


def save():
    (TMP / "external-go.json").write_text(json.dumps(OUT, indent=2) + "\n")


def diagnostics(data):
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", data.decode(errors="replace"))
    lines = []
    for line in text.splitlines():
        if re.search(r"authorization|api[-_]key|token=|secret|credential|password|://|remote_header|bes_header", line, re.I):
            continue
        match = re.search(r"(?:^|/)([A-Za-z0-9_.-]+\.go:(?:\d+(?::\d+)?:|\s+import of)\s*[^\n]{1,400})", line)
        if match:
            item = match[1]
        elif "missing strict dependencies" in line:
            item = "missing strict dependencies"
        else:
            continue
        if item not in lines and len(lines) < 12:
            lines.append(item)
    paths = sorted(set(re.findall(r"""['"]?(/usr/(?:include|lib/gcc)/[A-Za-z0-9./_+-]+[.]h)\b""", text)))[:12]
    return {"sha256": sha256(data).hexdigest(), "size": len(data), "go_errors": lines, "absolute_headers": paths}


def original_log(invocation):
    request = {"selector": {"invocation_id": invocation}}
    chunks = []
    for _ in range(20):
        page = json.loads(fetch("api/v1/GetLog", request))
        log = page["log"]
        if log.get("id"):
            assert log["id"].get("invocationId", log["id"].get("invocation_id")) == invocation
        chunks.append(log.get("contents", ""))
        token = page.get("nextPageToken", page.get("next_page_token"))
        if not token:
            return diagnostics("".join(chunks).encode())
        request["page_token"] = token
    raise ValueError("log exceeds bound")


def actions(target):
    rows = rpc("GetExecution", {"executionLookup": {"invocationId": UUID, "targetLabel": target},
                                 "inlineExecuteResponse": True}).get("execution", [])
    output = []
    for row in rows:
        if row.get("targetLabel") != target or row.get("actionMnemonic") != "GoCompilePkg":
            continue
        response = row.get("executeResponse", {})
        result = response.get("result", {})
        if result.get("exitCode") != 1 or row["stage"] not in (4, "COMPLETED"):
            continue
        original = {"action": row["actionDigest"], "exit": 1, "cached": response.get("cachedResult", False)}
        output.append(original)
        for stream in ("stdout", "stderr"):
            digest = result.get(stream + "Digest", {})
            data = (blob(digest, UUID) if int(digest.get("sizeBytes", 0)) else
                    base64.b64decode(result.get(stream + "Raw", ""), validate=True))
            original[stream] = diagnostics(data)
    return output


def observe(key, fn, arg):
    try:
        OUT["observations"][key] = fn(arg)
    except Exception as err:
        OUT["observations"][key] = {"error": type(err).__name__}
    save()


p = TMP / "shim.zip"
assert sha256(p.read_bytes()).hexdigest() == ARCHIVE
with ZipFile(p) as z:
    assert z.namelist() == ["external-go.json"]
    prior = json.loads(z.read("external-go.json"))
builds = prior["builds"]
assert prior["verdict"] == "INCOMPLETE" and builds["stock_ignore"]["state"] == "BUILT"
assert builds["split_ignore"]["invocation"] == UUID and builds["split_ignore"]["source"] == SPLIT
targets = [PREFIX + part + ":" + part for part in ("cgroup1", "cgroup2")]
assert sorted(f[0] for f in builds["split_ignore"]["failures"]) == targets
OUT.update(original_run=41399809619, artifact=13122060478, original_finished=prior["finished"],
           original_states={k: [r["source"], r["invocation"], r["state"], r["exit"]] for k, r in builds.items()})
save()
for target in targets:
    observe(target, actions, target)
for arm in ("split_ignore", "ordinary", "adopting"):
    observe(arm, original_log, builds[arm]["invocation"])
found = []
for target in targets:
    rows = OUT["observations"][target]
    names = target.split(":")[-1]
    if isinstance(rows, list) and any(r[s]["go_errors"] for r in rows for s in ("stdout", "stderr")):
        found.append(names)
OUT["compiler_owners_with_diagnostic"] = found
OUT["verdict"] = "RECOVERED" if len(found) == len(targets) else "INCOMPLETE"
OUT["finished"] = datetime.now(timezone.utc).isoformat()
save()
raise SystemExit(OUT["verdict"] != "RECOVERED")
