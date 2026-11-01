"""Replay the two failed Bazel kernel analyses with a 2 GB worker request."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import uuid

from cas_inputs import cas, rpc, TMP

ORIGINAL = "2565546b-e2c3-43d7-b70c-e342086ad2e8"
ACTIONS = {
    "k8": "bd6a0ac452efc51c2925eba167af4f102ccf0e991bc7b5ad536a5c68d31b2ae9",
    "aarch64": "77d1a6dc521a4c3d79c4a92181b1e81f8974d318227456ba8960958623c5777b",
}
KEY = os.environ["BUILDBUDDY_API_KEY"]


def now():
    return datetime.now(timezone.utc).isoformat()


def metrics(meta):
    return {
        "estimated_bytes": meta.get("estimatedTaskSize", {}).get("estimatedMemoryBytes"),
        "peak_bytes": meta.get("usageStats", {}).get("peakMemoryBytes"),
        "started": meta.get("executionStartTimestamp"),
        "finished": meta.get("executionCompletedTimestamp"),
    }


def replay(arch, row):
    digest = ACTIONS[arch]
    rows = rpc("GetExecution", {"executionLookup": {"invocationId": ORIGINAL, "actionDigestHash": digest}})["execution"]
    assert rows and all(r["actionDigest"]["hash"] == digest for r in rows)
    killed = [r for r in rows if r.get("status", {}).get("code") == 8
              and "signal: killed" in r["status"]["message"]]
    assert killed and all(r["targetLabel"] == "//pkg/sentry/kernel:kernel"
                          and r["actionMnemonic"] == "GoStaticAnalysis" for r in killed)
    row["original_failures"] = [metrics(r["executedActionMetadata"]) for r in killed]
    action = cas(killed[0]["actionDigest"], "Action")
    command = cas(action["commandDigest"], "Command")
    root = action["inputRootDigest"]
    row.update(input_root=root, original_command=action["commandDigest"])
    assert not command.get("workingDirectory") and not command.get("outputNodeProperties")
    assert not (command.get("outputPaths") and (command.get("outputFiles") or command.get("outputDirectories")))
    outputs = command.get("outputPaths") or command["outputFiles"]
    assert len(outputs) == 2 and not command.get("outputDirectories")
    assert {Path(p).name for p in outputs} == {"kernel.facts", "kernel.findings"}
    assert all(p.startswith("bazel-out/" + arch + "-") for p in outputs)
    platform = action.get("platform") or command["platform"]
    props = {p["name"]: p["value"] for p in platform["properties"]}
    memory = [k for k in props if k.lower() == "estimatedmemory"]
    row["original_memory_property"] = props.pop(memory[0]) if memory else None
    props["EstimatedMemory"] = "2GB"
    row["stage"] = "execute"
    response = TMP / (arch + "-private-response.json")
    invocation = str(uuid.uuid4())
    row["replay_invocation"] = invocation
    args = [str(TMP / "bb"), "execute", "--remote_executor=grpcs://remote.buildbuddy.io",
            "--remote_header=x-buildbuddy-api-key=" + KEY, "--invocation_id=" + invocation,
            "--input_root_digest=" + root["hash"] + "/" + str(root["sizeBytes"]),
            "--output=id", "--response_json_file=" + str(response), "--remote_timeout=15m",
            "--cache_read=false", "--cache_write=false"]
    args.extend("--exec_properties=" + k + "=" + v for k, v in sorted(props.items()))
    args.extend("--action_env=" + e["name"] + "=" + e.get("value", "") for e in command.get("environmentVariables", []))
    args.extend("--output_path=" + p for p in outputs)
    p = subprocess.run([*args, "--", *command["arguments"]], capture_output=True, timeout=940)
    row["client_exit"] = p.returncode
    reply = json.loads(response.read_bytes())
    result = reply.get("result", {})
    row.update(metrics(result.get("executionMetadata", {})))
    row.update(rpc_status=reply.get("status", {}).get("code", 0),
               exit=result.get("exitCode", 0), cached=reply.get("cachedResult", False))
    if row["rpc_status"] or row["exit"]:
        row["verdict"] = "FAIL"
    files = {f["path"]: f["digest"] for f in result.get("outputFiles", [])}
    row["output_digests"] = {Path(path).name: files[path] for path in outputs if path in files}
    assert row["started"] and row["finished"] and not row["cached"]
    assert row["client_exit"] == row["rpc_status"] == row["exit"] == 0
    assert files.keys() == set(outputs) and int(row["output_digests"]["kernel.facts"]["sizeBytes"]) > 0
    row["verdict"] = "PASS"


def check(arch):
    row = {"arch": arch, "original_action": ACTIONS[arch], "verdict": "INCOMPLETE", "stage": "read original"}
    try:
        replay(arch, row)
    except Exception as error:
        # Subprocess exceptions can contain credentials in the original arguments.
        import traceback
        row["error"] = type(error).__name__
        row["line"] = traceback.extract_tb(error.__traceback__)[-1].lineno
    return row


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(check, ACTIONS))
    success = all(r["verdict"] == "PASS" for r in rows)
    complete = [r["finished"] for r in rows if r.get("finished")]
    verdict = {"source": "14a4e93a872bf6dd49893b6bd9a5f55517167e2b", "original_invocation": ORIGINAL,
               "label": "//pkg/sentry/kernel:kernel", "memory_request": "2GB",
               "expected": list(ACTIONS), "rows": rows, "missing": [r["arch"] for r in rows if not r.get("finished")],
               "skipped": [], "finished": max(complete, default=None), "verified": now(),
               "verdict": "FAIL" if any(r["verdict"] == "FAIL" for r in rows) else ("PASS" if success else "INCOMPLETE")}
    (TMP / "analyzer-memory.json").write_text(json.dumps(verdict, indent=2) + "\n")
    raise SystemExit(0 if success else 1)
