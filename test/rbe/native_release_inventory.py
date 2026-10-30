"""Audit an existing build's physical executions without replaying its actions."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
from pathlib import Path

from qualification_report import Owner, advertised_build_outputs, compact_records, spawn_output_files


SOURCE = os.environ["GITHUB_SHA"]
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
ROOT = Path(os.environ["RUNNER_TEMP"])
OUTPUT = ROOT / "publication/release-inventory.json"


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
    row = ROOT / "qualification"
    stage, = row.glob("invocation-*/events.jsonl")
    assert (row / "final-head.txt").read_text().strip() == SOURCE
    verdict = json.loads((stage.parent / "verdict.json").read_text())
    report.update({k: verdict[k] for k in ("status", "native_exit", "capture_exit", "expected", "failures", "missing", "execution_finished_at")})
    with (ROOT / "spawns.json").open("w") as decoded:
        subprocess.run([os.environ["BB_LOG_DECODER"], "print", f"--compact_execution_log={stage.parent / 'execution.binpb'}", "--raw=true", "--sort=false"], stdout=decoded, check=True)
    events = [json.loads(s) for s in stage.read_text().splitlines()]
    report["invocation"], = [e["started"]["uuid"] for e in events if "started" in e]
    if verdict["native_exit"] or verdict["status"] != "PASS":
        _, partial = compact_records(ROOT / "spawns.json")
        report["failed_actions"] = [{"owner": p.get("targetLabel"), "mnemonic": p.get("mnemonic"), "exit": p.get("exitCode", 0), "runner": p.get("runner")} for p in partial if p.get("exitCode") or p.get("status")]
        raise ValueError("Native build did not qualify: " + verdict["status"])
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
    assert not report["missing"] and not report["failures"] and report["capture_exit"] == 0
    assert len(links) >= 2 and all(l["class"] == "scratch-arm64" for l in links)
    assert all(len(c["identified_runsc_link_indices"]) == 1 for c in report["closures"].values())
    assert len({c["identified_runsc_link_indices"][0] for c in report["closures"].values()}) == 2
    # A successful release can be mostly cache hits. This run must
    # independently execute every recorded action on the ARM64 worker.
    totals = Counter()
    for classes in report["all_spawns"].values():
        totals.update(classes)
    report["physical_execution"] = dict(totals)
    assert totals == {"scratch-arm64": len(spawns)}, totals
    for name, c in report["closures"].items():
        arch = name.split("/artifacts/")[1].split("/")[0]
        output_prefix = {"amd64": "bazel-out/k8-", "arm64": "bazel-out/aarch64-"}[arch]
        index, = c["identified_runsc_link_indices"]
        link, = (l for l in links if l["index"] == index)
        executable, = link["outputs"]
        assert executable.startswith(output_prefix), (name, executable)
    report["status"] = "cold-native-verified"


report = {"source": SOURCE, "status": "incomplete"}
try:
    audit(report)
except Exception as e:
    report["error"] = repr(e)
    report["status"] = "incomplete"
    raise
finally:
    report["verified"] = datetime.now(timezone.utc).isoformat()
    OUTPUT.write_text(json.dumps(report, indent=2) + "\n")
