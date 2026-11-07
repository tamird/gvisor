"""Compare the configured Go TestRunner actions with the native presubmit inventory."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


SOURCE = "4762045567775f1d258cc0c8cbe167e46e1b1a19"
OWNER_SHA256 = "8d77c4972ee4660d4ebc8517d8042f2ad8aa2ea1e9711ce0138d0860bf1cfd1b"
SHARDS = {
    "//pkg/tcpip/stack:stack_x_test": 8,
    "//pkg/tcpip/transport/tcp/test/e2e:tcp_test": 4,
}
# The six original Firecracker assignments are in artifact 13550358254.
# The Python SDK and native syscall test are outside the selected Go rule kind.
FIRECRACKER = {
    "//examples/sandboxexec/go/docprocessor:docprocessor_test",
    "//runsc/cmd:cmd_test",
    "//runsc/sandbox:sandbox_test",
    "//sandboxexec/sandbox:sandbox_test",
}
IMAGE = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
ARCHES = ("amd64", "arm64")
WORK = Path(os.environ["RUNNER_TEMP"], "mixed-go-graph")
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx8g"]
LABEL = re.compile(r"//[A-Za-z0-9_+./-]*:[A-Za-z0-9_+./-]+")
DIGEST = re.compile(r"[a-f0-9]{64}")


def save(report):
    (WORK / "receipt.json").write_text(json.dumps(report, sort_keys=True) + "\n")


def diagnostics(log):
    """Retain the first source diagnostics, with checkout and output paths removed."""
    results = []
    for line in log.read_text(errors="replace").splitlines():
        if not line.startswith(("ERROR:", "FATAL:", "Caused by:")):
            continue
        message = line.replace(str(Path.cwd()) + "/", "").replace(str(WORK), "<analysis>")
        message = re.sub(r"/home/runner/[^\s:]*?/external/", "external/", message)
        message = re.sub(r"/(?:home/runner|tmp|opt|usr)/[^\s:]+", "<runner-path>", message)
        message = re.sub(r"https?://[^\s]+", "<URL>", message)
        message = message[:750]
        if message not in results:
            results.append(message)
        if len(results) == 24:
            break
    return results


def query(kind, expression, options):
    query_file = WORK / (kind + ".query")
    output_file = WORK / (kind + ".out")
    log_file = WORK / (kind + ".log")
    query_file.write_text(expression + "\n")
    command = ["timeout", "--signal=INT", "--kill-after=45s", "24m", *BAZEL,
               kind, "--color=no", "--curses=no", "--keep_going", "--noshow_progress",
               *options, "--query_file=" + str(query_file)]
    with output_file.open("w") as out, log_file.open("w") as log:
        code = subprocess.run(command, stdout=out, stderr=log).returncode
    return output_file, {"exit": code, "diagnostics": diagnostics(log_file)}


def shard_path(action, artifacts, fragments):
    paths = []
    for output_id in action.get("outputIds", []):
        part = fragments[artifacts[str(output_id)]]
        if part["label"] != "test.log":
            continue
        components = [part["label"]]
        while part.get("parentId"):
            part = fragments[str(part["parentId"])]
            components.append(part["label"])
        paths.append(components)
    if len(paths) != 1:
        return None
    run, shard, total = 1, 1, 1
    for component in paths[0]:
        number = re.fullmatch(r"shard_(\d+)_of_(\d+)", component)
        if number:
            shard, total = map(int, number.groups())
        number = re.fullmatch(r"run_(\d+)_of_(\d+)", component)
        if number:
            run = int(number[1])
    return run, shard, total


def action_rows(graph, variants):
    labels = {str(t["id"]): t["label"] for t in graph.get("targets", [])}
    configs = {str(c["id"]): c.get("checksum") for c in graph.get("configuration", [])}
    fragments = {str(f["id"]): f for f in graph.get("pathFragments", [])}
    artifacts = {str(a["id"]): str(a["pathFragmentId"]) for a in graph.get("artifacts", [])}
    rows = []
    auxiliary = Counter()
    for action in graph.get("actions", []):
        label = labels[str(action["targetId"])]
        assert LABEL.fullmatch(label) and action["mnemonic"] == "TestRunner"
        physical_shard = shard_path(action, artifacts, fragments)
        if physical_shard is None:
            auxiliary[label] += 1
            continue
        run, shard, total = physical_shard
        platform = action.get("executionPlatform", "").removeprefix("@@")
        properties = {p["key"]: p.get("value", "") for p in action.get("executionInfo", [])}
        # These named platforms declare Linux, OCI, the image and the architecture.
        # Action-specific properties override those defaults for namespace tests.
        owner, cpu = variants.get(label, ("", ""))
        isolation = properties.get("workload-isolation-type", "oci")
        root = properties.get("dockerUser") == "root"
        ipv6 = properties.get("network-enable-ipv6") == "true"
        on_worker = (platform == "//test/rbe:ubuntu_" + cpu
                     and properties.get("Arch", cpu) == cpu
                     and properties.get("OSFamily", "linux") == "linux"
                     and properties.get("container-image", IMAGE) == IMAGE)
        namespace = owner in FIRECRACKER
        placed = on_worker and (isolation == "firecracker" and root and ipv6
                                if namespace else isolation == "oci")
        checksum = configs.get(str(action.get("configurationId")))
        rows.append({
            "label": label, "config": checksum if DIGEST.fullmatch(checksum or "") else None,
            "run": run, "shard": shard, "total_shards": total,
            "execution_platform": platform if LABEL.fullmatch(platform) else None,
            "isolation": isolation if isolation in ("oci", "firecracker") else "other",
            "root": root, "ipv6": ipv6, "placement_matches": placed,
        })
    return rows, dict(auxiliary)


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    owner_file = WORK / "owners.txt"
    assert hashlib.sha256(owner_file.read_bytes()).hexdigest() == OWNER_SHA256
    owners = owner_file.read_text().splitlines()
    assert len(set(owners)) == len(owners) == 363 and all(LABEL.fullmatch(o) for o in owners)
    report = {"source": SOURCE, "original_artifact": 13550377161,
              "original_placement_artifact": 13550358254, "original_owner_sha256": OWNER_SHA256,
              "original_owners": len(owners), "verified": False}
    save(report)
    output, report["kind_query"] = query(
        "query", 'kind("^go_test rule$", set(' + " ".join(owners) + '))', ["--output=label"])
    go = sorted(output.read_text().splitlines())
    report["go_owners"] = go
    save(report)
    assert report["kind_query"]["exit"] == 0 and len(set(go)) == len(go) == 340
    assert set(go) <= set(owners) and SHARDS.keys() <= set(go) and FIRECRACKER <= set(go)
    variants = {owner + "_" + arch: (owner, arch) for owner in go for arch in ARCHES}
    events = WORK / "graph.bep"
    graph_file, report["aquery"] = query(
        "aquery", 'mnemonic("^TestRunner$", set(' + " ".join(variants) + '))',
        ["--config=source-presubmit-mixed", "--spawn_strategy=local", "--strategy=TestRunner=local",
         "--remote_executor=", "--remote_cache=", "--bes_backend=", "--jobs=4",
         "--output=jsonproto", "--include_artifacts=true", "--noinclude_commandline",
         "--build_event_json_file=" + str(events)])
    save(report)
    if events.exists():
        with events.open() as stream:
            report["execution_events"] = sum(
                int(any(key in event for key in ("action", "testResult", "testSummary")))
                for line in stream if line.strip() for event in [json.loads(line)])
    save(report)
    # Return the Bazel server's memory before reading the full mixed-CPU graph.
    subprocess.run([*BAZEL, "shutdown"], check=False, timeout=75)
    if not graph_file.stat().st_size:
        raise RuntimeError("aquery produced no graph")
    with graph_file.open() as stream:
        rows, auxiliary = action_rows(json.load(stream), variants)
    expected = Counter((label, 1, i, SHARDS.get(owner, 1))
                       for label, (owner, _) in variants.items()
                       for i in range(1, SHARDS.get(owner, 1) + 1))
    actual = Counter((r["label"], r["run"], r["shard"], r["total_shards"]) for r in rows)
    report.update(variants=len(variants), expected_shards=expected.total(), rows=rows,
                  auxiliary_without_unique_test_log=auxiliary,
                  execution_platforms=dict(Counter(r["execution_platform"] or "other" for r in rows)),
                  isolation=dict(Counter(r["isolation"] for r in rows)),
                  missing=list((expected - actual).elements()), extra=list((actual - expected).elements()))
    report["verified"] = (report["aquery"]["exit"] == 0 and report.get("execution_events") == 0
                          and len(variants) == 680 and len(rows) == expected.total() == 700
                          and actual == expected and all(r["placement_matches"] and r["config"] for r in rows))
    save(report)
    print("Expected", expected.total(), "observed", len(rows), report["isolation"],
          report["execution_platforms"], flush=True)
    assert report["verified"]


if __name__ == "__main__":
    main()
