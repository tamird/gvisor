"""Read Bazel's cgroup execution requirements and actual runner records."""
import json
import os
import re
import sys
import zipfile
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
    if any(name == owner + "_nogo" and k == "TestRunner" and ("local" in info or "no-sandbox" in info) for name, k, info in rows):
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


def replay(archive, destination):
    t = owner + "_nogo"
    with zipfile.ZipFile(archive) as z:
        v, o, p = (json.loads(z.read("nogo-cgroup/"+n+".json")) for n in ("verdict","owners","placement"))
    assert v["source"] == os.environ["PRODUCT"] and v["invocation"] == "fbb7d83e-c7ce-4dfc-af91-4fb6c0e5618e"
    assert v["status"] == 0 and v["bazel_exit"]["name"] == "SUCCESS" and v["testRunners"] == 1
    assert o["configured"] == [t] and o["completed"] == {t: True} and o["summaries"] == {t:"PASSED"}
    assert o["markers"] == {t: {"amd64": True, "arm64": True}}
    assert o["results"] == {t: [{"status":"PASSED","strategy":"processwrapper-sandbox","cachedLocally":False,"cachedRemotely":False}]}
    req = p["requirements"]
    assert any(label(n) == owner and k == "TestRunner" and "local" in flags for n, k, flags in req)
    assert not any(label(n) == t and k == "TestRunner" and ("local" in flags or "no-sandbox" in flags) for n,k,flags in req)
    assert p["verified"]
    for k in ("GoCompilePkg","GoStaticAnalysis"):
        matches = [a for a in p["executions"] if label(a["targetLabel"]) == owner and a["mnemonic"] == k]
        assert len(matches) == 2 and all(a["runner"] == "local" and not a["cacheHit"] and not a["exitCode"] for a in matches), matches
    destination.mkdir(parents=True, exist_ok=True)
    v.update(verified=True, outcome="passed", originalReporterVerdict=False,
             reconciliation="Bazel and the named Nogo TestRunner passed. Local-only TestRunner requirements name the ordinary Go test, not this Nogo owner.")
    for n, value in (("verdict",v), ("owners",o), ("placement",p)):
        (destination/(n+".json")).write_text(json.dumps(value,indent=2)+"\n")


if __name__ == "__main__":
    if sys.argv[1] == "replay":
        replay(Path(sys.argv[2]), Path(sys.argv[3]))
    elif sys.argv[1] == "requirements":
        requirements()
    else:
        assert sys.argv[1] == "execution"
        execution()
