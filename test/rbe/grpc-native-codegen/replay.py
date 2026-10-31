"""Verify the original gRPC action results without executing Bazel again."""
import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

source = Path(sys.argv[1])
digest = hashlib.sha256(source.read_bytes()).hexdigest()
assert digest == "8f5e4794616bd06101f4cf9d04a2d335c5090d99b6942678598519c1fca3dbde", digest
with zipfile.ZipFile(source) as archive:
    def read(name):
        return json.loads(archive.read(name + ".json"))

    original = read("verdict")
    assert original["status"] == "FAIL"
    assert list(original["errors"]) == ["stock-scratch"]
    assert set(original["lanes"]) == {"native", "ordinary"}
    targets = set(original["targets"])
    assert len(targets) == 2
    for name in ("native", "ordinary"):
        assert read(name + "-run")["exit"] == 0
        events = [e for e in read(name + "-executions") if e["mnemonic"] == "Action"]
        assert {e["targetLabel"] for e in events} == targets
        assert len(events) == len(targets)
        for event in events:
            assert event["runner"] == "remote" and event["exitCode"] == 0
            assert not event["cacheHit"] and not event["remoteCacheHit"]
            props = {p["name"]: p["value"] for p in event["platform"]["properties"]}
            assert props["Arch"] == "amd64"
            assert props["container-image"].startswith("docker://" + ("mirror.gcr.io/library/hello-world@" if name == "native" else "gcr.io/gvisor-presubmit/default_x86_64@"))
    outputs = read("ordinary-outputs")
    assert outputs == read("native-outputs") and len(outputs) == 4
    assert all(item["size"] > 0 for item in outputs.values())
    tools = read("native-executables")
    assert len(tools) == 2
    assert all(not t["contains_interp"] and not t["contains_needed"] for t in tools.values())
    negative = read("stock-scratch-run")
    copy, = read("stock-scratch-executions")
    assert negative["exit"] == 1 and copy["runner"] == "remote"
    assert copy["targetLabel"] == "@@grpc+//src/compiler:grpc_cpp_plugin" and copy["mnemonic"] == "Genrule"
    assert copy["exitCode"] == 1 and copy["status"] == "NON_ZERO_EXIT" and not copy["actualOutputs"]
    assert copy["commandArgs"][:2] == ["/bin/bash", "-c"] and "; cp " in copy["commandArgs"][2]
    image = next(p["value"] for p in copy["platform"]["properties"] if p["name"] == "container-image")
    assert image == "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
    error, = [re.sub(r"\\+/", "/", line) for line in negative["evidence"] if "executable file" in line]
    assert "/bin/bash" in error and "not found" in error and "No such file or directory" in error
    result = {
        "status": "PASS", "source": original["gvisor_base"], "grpc_release": original["grpc_release"],
        "supplier_commit": "https://github.com/tamird/grpc/commit/184987c9a",
        "original_artifact": "https://github.com/tamird/gvisor/actions/runs/41230918674/artifacts/13053878288",
        "original_archive_sha256": digest, "original_workflow_status": "failure",
        "reason_for_replay": "The original text assertion did not decode escaped slashes in the OCI error.",
        "completed": datetime.datetime.now(datetime.timezone.utc).isoformat(), "targets": sorted(targets),
        "executions": {lane: read(lane + "-run") for lane in ("native", "ordinary", "stock-scratch")},
        "original_negative_reason": error, "matches": outputs, "static_tools": tools,
    }
    print(json.dumps(result, indent=2))
