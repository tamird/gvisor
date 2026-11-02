"""Read Bazel's cgroup execution requirements and actual runner records."""
import json
import os
import re
import sys
from pathlib import Path

root = Path(os.environ["RUNNER_TEMP"])
owner = "//runsc/cgroup:cgroup_test"
targets = {owner, owner + "_nogo"}
out = root / "nogo-cgroup"
receipt = out / "placement.json"


def label(value):
    return value.removeprefix("@@").removeprefix("@")


def requirements():
    query = json.loads((root / "cgroup-actions.json").read_text())
    labels = {t["id"]: label(t["label"]) for t in query.get("targets", [])}
    rows = []
    special = {"local", "no-remote", "no-remote-exec"}
    for action in query.get("actions", []):
        name = labels.get(action.get("targetId"))
        if name not in targets:
            continue
        info = {i["key"] for i in action.get("executionInfo", [])}
        if info & special:
            rows.append((name, action["mnemonic"], sorted(info & (special | {"no-sandbox"}))))
    kinds = {k for _, k, _ in rows}
    receipt.write_text(json.dumps({"configuredTargets":len(labels),"actions":len(query.get("actions", [])),"requirements":rows,"mnemonics":sorted(kinds)}, indent=2)+"\n")
    assert {"GoCompilePkg", "GoStaticAnalysis"} <= kinds, rows
    with (root / "nogo-resources.bazelrc").open("a") as f:
        for k in sorted(kinds):
            assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]*", k)
            # The TestRunner already has an explicitly local sandbox strategy.
            strategy = "sandboxed,local" if k == "TestRunner" else "remote,local"
            f.write(f"test --strategy={k}={strategy}\n")
    # A target with 'local' cannot use the processwrapper sandbox either.
    if any(k == "TestRunner" and ("local" in info or "no-sandbox" in info) for _, k, info in rows):
        with Path(os.environ["GITHUB_ENV"]).open("a") as f:
            f.write("NOGO_CGROUP_TEST_STRATEGY=local\n")


def spawns(f):
    decoder = json.JSONDecoder()
    buf = ""
    while chunk := f.read(1 << 20):
        buf += chunk
        while buf:
            buf = buf.lstrip()
            if not buf:
                break
            try:
                msg, pos = decoder.raw_decode(buf)
            except json.JSONDecodeError:
                break
            yield msg
            buf = buf[pos:]
    assert not buf.strip(), "incomplete execution record"


def execution():
    v = json.loads(receipt.read_text())
    rows = []
    with (root / "cgroup-spawns.json").open() as f:
        for a in spawns(f):
            if label(a.get("targetLabel", "")) in targets:
                rows.append({k: a.get(k) for k in ("targetLabel","mnemonic","runner","cacheHit","exitCode")})
    v["executions"] = rows
    v["verified"] = False
    receipt.write_text(json.dumps(v,indent=2)+"\n")
    for k in ("GoCompilePkg","GoStaticAnalysis"):
        assert sum(a["mnemonic"] == k and a["runner"] == "local" and
                   not a["cacheHit"] and not a["exitCode"] for a in rows) >= 2, rows
    v["verified"] = True
    receipt.write_text(json.dumps(v,indent=2)+"\n")


if __name__ == "__main__":
    if sys.argv[1] == "requirements":
        requirements()
    else:
        assert sys.argv[1] == "execution"
        execution()
