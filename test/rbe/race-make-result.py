"""Reconcile the two smoke executions with the four requested Bazel binaries."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform

work = Path(os.environ["RUNNER_TEMP"], "race-make")
source = os.environ["PRODUCT"]
labels = ["//runsc:runsc", "//runsc:runsc-race", "//runsc/cmd/sentry:gvisor_sentry", "//runsc/cmd/sentry:gvisor_sentry-race"]
releases = {"race": "//:release-race", "default": "//:release"}
guests = ["smoke-race-tests", "smoke-tests"]
expected_files = {"runsc", "containerd-shim-runsc-v1", "gvisor-bin/checkpointgofer", "gvisor-bin/runsc-metric-server", "gvisor-bin/runsc-fd-parking", "gvisor-bin/gvisor_sentry", "gvisor-bin/gvisor-sentry-prewarmer", "gvisor-bin/gvisor-proc-umounter"}
verdict = {"source": source, "carrier": os.environ["GITHUB_SHA"], "expectedTargets": [*labels, *releases.values()], "executedTargets": [], "expectedGuests": guests, "executedGuests": [], "failures": [], "skips": [], "nativeMachine": platform.machine(), "builds": {}, "staged": {}, "startedAt": (work / "start").read_text().strip(), "executionFinishedAt": (work / "finish").read_text().strip()}
try:
    assert os.getuid() != 0 and platform.machine() == "x86_64"
    assert os.environ["GLIBC_TUNABLES"] == "glibc.pthread.rseq=0"
    stamps = set()
    for arm, targets in [("binaries", labels), *((flavor, [label]) for flavor, label in releases.items())]:
        record = {"executed": [], "exit": int((work / f"{arm}.status").read_text())}
        verdict["builds"][arm] = record
        bep = work / f"{arm}.bep"
        if bep.exists():
            for line in bep.open():
                event = json.loads(line)
                if "started" in event:
                    record["invocation"] = event["started"]["uuid"]
                if "workspaceStatus" in event:
                    record["stamp"] = next((item["value"] for item in event["workspaceStatus"].get("item", []) if item["key"] == "STABLE_VERSION"), None)
                label = event["id"].get("targetCompleted", {}).get("label")
                if label in targets and event.get("completed", {}).get("success"):
                    record["executed"].append(label)
                if "finished" in event:
                    record["buildExit"] = event["finished"]["exitCode"].get("code", 0)
        verdict["executedTargets"].extend(record["executed"])
        if record["exit"] or record.get("buildExit") != 0 or set(record["executed"]) != set(targets):
            verdict["failures"].append(arm)
        if arm in releases and record["exit"] == 0:
            log = (work / f"{arm}.log").read_text(errors="replace")
            matched = [line for line in log.splitlines() if 'in sidecar "gvisor_sentry"' in line and "matches the expected release" in line and source[:12] in line]
            if not matched or 'Sidecar "gvisor_sentry" found: booting sandbox' not in log or "Changing configuration RootDir" not in log:
                verdict["failures"].append(f"{arm} guest did not boot its matching Sentry")
            else:
                verdict["executedGuests"].append("smoke-race-tests" if arm == "race" else "smoke-tests")
        stamps.add(record.get("stamp"))
    assert not verdict["failures"], "incomplete build or guest"
    assert len(stamps) == 1 and source[:12] in next(iter(stamps))
    paths = [Path(line) for line in (work / "binaries.paths").read_text().splitlines()]
    assert {p.name for p in paths} == {label.split(":")[-1] for label in labels} and len(paths) == len(labels)
    digests = {p.name: hashlib.file_digest(p.open("rb"), "sha256").hexdigest() for p in paths}
    for flavor in releases:
        install = work / flavor
        assert {str(p.relative_to(install)) for p in install.rglob("*") if p.is_file()} == expected_files
        proofs = verdict["staged"][flavor] = {}
        for name in ["runsc", "gvisor-bin/gvisor_sentry"]:
            data = (install / name).read_bytes()
            original = Path(name).name + ("-race" if flavor == "race" else "")
            proof = proofs[name] = {"configuredOutput": original, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "raceInit": b"runtime.raceinit" in data, "tsan": b"ThreadSanitizer" in data}
            assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == 62
            assert proof["sha256"] == digests[original], f"{name} differs from {original}"
            assert proof["raceInit"] == (flavor == "race") and proof["tsan"] == (flavor == "race"), f"{flavor} {name} has wrong race instrumentation"
except Exception as err:
    verdict["failures"].append(f"{type(err).__name__}: {err}")
finally:
    verdict["missingTargets"] = sorted(set(verdict["expectedTargets"]) - set(verdict["executedTargets"]))
    verdict["missingGuests"] = sorted(set(guests) - set(verdict["executedGuests"]))
    verdict["accepted"] = not (verdict["failures"] or verdict["missingTargets"] or verdict["missingGuests"])
    verdict["verdictAt"] = datetime.now(timezone.utc).isoformat()
    (work / "records" / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
print("accepted", verdict["accepted"], verdict["failures"])
raise SystemExit(not verdict["accepted"])
