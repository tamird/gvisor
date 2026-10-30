"""Audit an existing build's physical executions without replaying its actions."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from qualification_report import Owner, advertised_build_outputs, compact_records, spawn_output_files


SOURCE = "dc905d2e049bdd6f3a5da40d9fba233b2a2aecee"
INVOCATION = "46b7348c-bd0b-41e3-9480-4ce4635e6c1f"
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
ROOT = Path(os.environ["RUNNER_TEMP"])
OUTPUT = ROOT / "out/inventory.json"


def classify(spawn):
    if spawn.get("exitCode", 0) or spawn.get("status"):
        return "failed"
    if spawn.get("cacheHit") or spawn.get("remoteCacheHit") or "cache" in spawn.get("runner", "").lower():
        return "cache"
    props = {p["name"]: p["value"] for p in spawn.get("platform", {}).get("properties", [])}
    if spawn.get("runner") == "remote" and spawn.get("digest") and props.get("container-image") == IMAGE:
        return "scratch-" + props.get("Arch", "unknown")
    return "other-" + (spawn.get("runner") or "unknown")


def audit(report):
    row = ROOT / "row"
    stage, = row.glob("invocation-*/events.jsonl")
    assert (row / "final-head.txt").read_text().strip() == SOURCE
    assert int((stage.parent / "native-exit.txt").read_text()) == 0
    events = [json.loads(s) for s in stage.read_text().splitlines()]
    assert [e["started"]["uuid"] for e in events if "started" in e] == [INVOCATION]
    owners = {Owner.parse(e["id"]["targetCompleted"]) for e in events if "completed" in e
              and e["id"]["targetCompleted"].get("label") == "//test/release:artifacts"
              and not e["id"]["targetCompleted"].get("aspect") and e["completed"].get("success")}
    assert len(owners) == 1
    outputs = advertised_build_outputs(stage, {(o, "default") for o in owners})
    assert len(outputs) == 6
    report["expected_outputs"] = [{k: o[k] for k in ("name", "sha256")} for o in outputs]
    records, spawns = compact_records(ROOT / "spawns.json")
    assert spawns
    producers = defaultdict(set)
    paths = defaultdict(set)
    by_mnemonic = defaultdict(Counter)
    links = []
    for i, s in enumerate(spawns):
        category = classify(s)
        by_mnemonic[s.get("mnemonic", "<unknown>")][category] += 1
        output_ids = [o["outputId"] for o in s.get("outputs", []) if "outputId" in o]
        for rid in output_ids:
            producers[rid].add(i)
        output_files = spawn_output_files(records, s)
        for path, meta in output_files.items():
            paths[(path, meta["sha256"])].add(i)
        if s.get("mnemonic") == "GoLink" and s.get("targetLabel", "").startswith("//runsc:"):
            links.append({"index": i, "label": s["targetLabel"], "class": category,
                          "outputs": output_files, "digest": s.get("digest")})
    report["all_spawns"] = dict(sorted(by_mnemonic.items()))
    report["runsc_links"] = links
    report["total_spawns"] = len(spawns)

    # Trace only the release closures that can be established from recorded
    # inputs. A derived input with no recorded producer is a gap, not an
    # implied execution on the worker used by its consuming action.
    def closure(seeds):
        pending_spawns = list(seeds)
        pending_inputs = []
        seen_spawns = set()
        seen_inputs = set()
        gaps = Counter()
        while pending_spawns or pending_inputs:
            if pending_spawns:
                i = pending_spawns.pop()
                if i in seen_spawns:
                    continue
                seen_spawns.add(i)
                s = spawns[i]
                pending_inputs.extend(s[f] for f in ("inputSetId", "toolSetId") if s.get(f))
                continue
            rid = pending_inputs.pop()
            if rid in seen_inputs:
                continue
            seen_inputs.add(rid)
            r = records[rid]
            if rid in producers:
                pending_spawns.extend(producers[rid])
            elif "file" in r and r["file"]["path"].startswith("bazel-out/"):
                path = r["file"]["path"]
                matches = paths.get((path, r["file"].get("digest", {}).get("hash")))
                if matches:
                    pending_spawns.extend(matches)
                else:
                    gaps["/".join(path.split("/")[:4])] += 1
            if "inputSet" in r:
                pending_inputs.extend(r["inputSet"].get("inputIds", []))
                pending_inputs.extend(r["inputSet"].get("transitiveSetIds", []))
            if "runfilesTree" in r:
                pending_inputs.append(r["runfilesTree"]["inputSetId"])
        counts = Counter(classify(spawns[i]) for i in seen_spawns)
        return {"spawns": dict(counts), "untraced_generated_file_inputs": sum(gaps.values()),
                "largest_untraced_groups": gaps.most_common(5),
                "identified_runsc_link_indices": sorted(l["index"] for l in links if l["index"] in seen_spawns)}

    report["closures"] = {}
    for o in outputs:
        name, digest = o["name"], o["sha256"]
        direct = paths.get((name, digest), set())
        if not direct:
            direct = {i for (p, h), owners in paths.items() if h == digest and p.endswith("/" + name) for i in owners}
        assert direct, (name, digest)
        report["closures"][name] = closure(direct)
    report["original_log_sha256"] = hashlib.sha256((stage.parent / "execution.binpb").read_bytes()).hexdigest()
    report["original_execution_finished"] = (stage.parent / "execution-finished.txt").read_text().strip()
    # This is an evidence inventory, not a reclassification of the parent
    # wrapper or an all-scratch pass when cached/missing provenance remains.
    report["status"] = "inventory-verified"


report = {"source": SOURCE, "invocation": INVOCATION, "status": "incomplete"}
try:
    audit(report)
except Exception as e:
    report["error"] = str(e)
    raise
finally:
    report["verified"] = datetime.now(timezone.utc).isoformat()
    OUTPUT.write_text(json.dumps(report, indent=2) + "\n")
