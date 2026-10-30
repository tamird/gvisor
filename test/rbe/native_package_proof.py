"""Check the same tar writer that failed to start on the empty ARM64 worker."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

from qualification_report import Owner, advertised_build_outputs, compact_records, spawn_output_files

root = Path(os.environ["RUNNER_TEMP"])
label = "//shim:config"
result = {"source": os.environ["GITHUB_SHA"], "status": "incomplete"}
try:
    row = root / "qualification"
    assert (row / "final-head.txt").read_text().strip() == result["source"]
    log, = row.glob("invocation-*/execution.binpb")
    stage = log.parent
    verdict = json.loads((stage / "verdict.json").read_text())
    result.update({k: verdict[k] for k in ("expected", "native_exit", "capture_exit", "missing", "failures", "execution_finished_at")})
    assert verdict["status"] == "PASS" and result["native_exit"] == result["capture_exit"] == 0, verdict["status"]
    assert not result["missing"] and not result["failures"] and [t["label"] for t in result["expected"]] == [label]
    events = stage / "events.jsonl"
    owners = set()
    with events.open() as stream:
        for line in stream:
            e = json.loads(line)
            if "started" in e:
                result["invocation"] = e["started"]["uuid"]
            t = e.get("id", {}).get("targetCompleted", {})
            if t.get("label") == label and not t.get("aspect") and e.get("completed", {}).get("success"):
                owners.add(Owner.parse(t))
    output, = advertised_build_outputs(events, {(owner, "default") for owner in owners})
    path = Path(output["name"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == output["sha256"]
    with tarfile.open(path) as archive:
        member, = [m for m in archive if m.isfile()]
        assert member.name.removeprefix("./") == "etc/containerd/runsc.toml" and member.mode == 0o644, (member.name, member.mode)
        assert archive.extractfile(member).read() == Path("shim/runsc.toml").read_bytes()
    decoded = root / "package-spawns.json"
    with decoded.open("w") as dst:
        subprocess.run([os.environ["BB_LOG_DECODER"], "print", f"--compact_execution_log={log}", "--raw=true", "--sort=false"], stdout=dst, check=True)
    records, spawns = compact_records(decoded)
    action, = [s for s in spawns if s.get("targetLabel") == label and s.get("mnemonic") == "PackageTar"]
    props = {p["name"]: p["value"] for p in action["platform"]["properties"]}
    executable = action["args"][0]
    result.update(executable=executable, platform=props.get("Arch"), image=props.get("container-image"),
                  runner=action.get("runner"), cached=bool(action.get("cacheHit") or action.get("remoteCacheHit")),
                  outputs=spawn_output_files(records, action))
    assert result["runner"] == "remote" and not result["cached"] and not action.get("exitCode") and not action.get("status")
    assert (props.get("Arch"), props.get("OSFamily"), props.get("container-image")) == ("arm64", "linux", "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef")
    assert "protoc_check_static_python_arm64" in executable and executable.endswith("/bin/python3.11"), executable
    assert any(f["sha256"] == output["sha256"] for f in result["outputs"].values())
    result["status"] = "pass"
except Exception as e:
    result["error"] = repr(e)
    raise
finally:
    result["verdict_at"] = datetime.now(timezone.utc).isoformat()
    (root / "publication/package-writer.json").write_text(json.dumps(result, indent=2) + "\n")
