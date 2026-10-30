"""Verify real ARM64 worker executions and both target ELF headers."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

from qualification_report import Owner, advertised_build_outputs, compact_records, spawn_output_files


root = Path(os.environ["RUNNER_TEMP"])
result = {"source": os.environ["GITHUB_SHA"], "status": "incomplete"}
try:
    row = root / "qualification"
    assert (row / "final-head.txt").read_text().strip() == result["source"]
    log, = row.glob("invocation-*/execution.binpb")
    stage = log.parent
    verdict = json.loads((stage / "verdict.json").read_text())
    result.update({k: verdict.get(k) for k in ("invocation", "native_exit", "capture_exit", "expected", "missing", "failures", "verified", "execution_finished_at")})
    assert verdict["status"] == "PASS" and result["verified"] and result["native_exit"] == result["capture_exit"] == 0
    assert not result["missing"] and not result["failures"]
    wanted = {"//vdso:vdso.so", "//pkg/abi/linux:linux", "//tools/package_version:package_version"}
    events = stage / "events.jsonl"
    owners = set()
    with events.open() as stream:
        for line in stream:
            e = json.loads(line)
            t = e.get("id", {}).get("targetCompleted", {})
            if t.get("label") in wanted and not t.get("aspect") and e.get("completed", {}).get("success"):
                owners.add(Owner.parse(t))
    assert {o.label for o in owners} == wanted
    outputs = advertised_build_outputs(events, {(o, "default") for o in owners})
    elfs = {}
    for label in ("//vdso:vdso.so", "//tools/package_version:package_version"):
        output, = [o for o in outputs if o["identity"]["label"] == label]
        path = Path(output["name"])
        with path.open("rb") as src:
            header = src.read(20)
        assert header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20], "little") == 183, label
        elfs[label] = {"sha256": output["sha256"], "bytes": output["bytes"], "machine": "AArch64"}
    result["elfs"] = elfs
    decoded = root / "compiler-spawns.json"
    with decoded.open("w") as dst:
        subprocess.run([os.environ["BB_LOG_DECODER"], "print", f"--compact_execution_log={log}", "--raw=true", "--sort=false"], stdout=dst, check=True)
    records, spawns = compact_records(decoded)
    required = {"CppCompile": "//vdso:", "CppLink": "//vdso:", "GoStateify": "//pkg/abi/linux:", "GoCompilePkg": "//pkg/abi/linux:linux", "GoLink": "//tools/package_version:package_version"}
    proof = {k: [] for k in required}
    result["executions"] = proof
    for s in spawns:
        m, lbl = s.get("mnemonic"), s.get("targetLabel", "")
        if m not in required or not lbl.startswith(required[m]):
            continue
        props = {p["name"]: p["value"] for p in s["platform"]["properties"]}
        image = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
        assert props["Arch"] == "arm64" and props["OSFamily"].lower() == "linux" and props["container-image"] == image, (m, lbl, props)
        assert s["runner"] == "remote" and not any(s.get(k) for k in ("cacheHit", "remoteCacheHit", "exitCode", "status")), (m, lbl, s["runner"])
        proof[m].append({"owner": lbl, "action": s["digest"]["hash"], "outputs": spawn_output_files(records, s)})
    assert all(proof.values()), {m: len(v) for m, v in proof.items()}
    result["status"] = "pass"
except Exception as exc:
    result["error"] = repr(exc)
    raise
finally:
    result["verdict_at"] = datetime.now(timezone.utc).isoformat()
    (root / "publication/arm64.json").write_text(json.dumps(result, indent=2) + "\n")
