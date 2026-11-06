"""Record only target results, ELF identity, and the expected rejection."""

import hashlib
import json
import os
from pathlib import Path
import re

work = Path(os.environ["RUNNER_TEMP"]) / "vdso-results"
result = {}
for phase in ("llvm", "negative", "arm-build", "arm-check", "standalone"):
    status = work / (phase + ".exit")
    record = {"exit": int(status.read_text()) if status.exists() else None}
    result[phase] = record
    log = work / (phase + ".log")
    if log.is_file():
        console = log.read_text(errors="replace")
        record["diagnostics"] = [s for s in (
            "No such file or directory", "No matching toolchains", "no such target",
            "VDSO contains relocations", "VDSO contains no/empty .text section",
            "No PT_LOAD segments in VDSO", "VDSO contains non-empty .data section",
        ) if s in console]
    path = work / (phase + ".bep")
    if not path.is_file():
        continue
    attempts = []
    with path.open() as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                record["incomplete_bep"] = True
                break
            ids = event["id"]
            if "started" in event:
                record["invocation"] = event["started"].get("uuid")
            if (ids.get("testResult", {}).get("label") == "//vdso:vdso_test"
                    and (attempt := event.get("testResult"))):
                info = attempt.get("executionInfo", {})
                attempts.append({
                    "status": attempt["status"],
                    "remote": info.get("strategy") == "remote",
                    "cached": bool(attempt.get("cachedLocally") or info.get("cachedRemotely")),
                })
                for output in attempt.get("testActionOutput", []):
                    if output["name"] == "test.log" and (cas := re.search(
                            r"/blobs/([a-f0-9]{64})/(\d+)$", output.get("uri", ""))):
                        record["test_log"] = [cas[1], int(cas[2])]
            if ids.get("targetCompleted", {}).get("label") == "//vdso:vdso":
                record["built"] = event.get("completed", {}).get("success", False)
    if phase != "arm-build":
        record["attempts"] = attempts

arm_file = work / "arm-vdso.so"
if arm_file.is_file():
    data = arm_file.read_bytes()
    result["arm-build"].update(
        aarch64=data.startswith(b"\x7fELF\x02\x01") and int.from_bytes(data[18:20], "little") == 183,
        sha256=hashlib.sha256(data).hexdigest(),
    )
expected_pass = [{"status": "PASSED", "remote": True, "cached": False}]
expected_failure = [{"status": "FAILED", "remote": True, "cached": False}]
verified = (
    all(result[p].get("attempts") == expected_pass and result[p]["exit"] == 0
        for p in ("llvm", "arm-check", "standalone"))
    and result["negative"].get("attempts") == expected_failure
    and "VDSO contains non-empty .data section" in result["negative"].get("diagnostics", [])
    and result["negative"]["exit"] == 3
    and result["arm-build"]["exit"] == 0
    and result["arm-build"].get("aarch64")
    and not any(row.get("incomplete_bep") for row in result.values())
)
result.update(
    verified=bool(verified),
    standalone_source="466dbf7a9a4ff6d12693d0fa94ba37c2bc353d48",
    llvm_source="f1d38511abda94cb9ff0a2627feef27a47d20382",
)
(work / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
