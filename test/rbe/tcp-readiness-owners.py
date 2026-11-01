"""Check exact-source TCP suites, the two actual Nogo sections and checkpoint boundary."""

import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlparse


root = Path(os.environ["RUNNER_TEMP"]) / "tcp-evidence"
src = os.environ["PRODUCT"]
parent = "254c8ddfcc97180f83ba6454db22afb6235b5df3"
e2e = "//pkg/tcpip/transport/tcp/test/e2e:"
tcp = "//pkg/tcpip/transport/tcp:"
owners = [tcp + "tcp_test"] + [
    e2e + target for target in
    "tcp_test dual_stack_test forwarder_test rcv_test sack_scoreboard_test tcp_timestamp_test tcp_rack_test tcp_acktiming_test tcp_sack_test tcp_noracedetector_test".split()
]
analyzers = [tcp + "tcp_nogo", e2e + "e2e_nogo", *(label + "_nogo" for label in owners)]
schema = ["//test/rbe/readiness_schema:schema_test"]
labels_by_phase = {"owners": owners, "analysis": analyzers, "parent-schema": schema, "product-schema": schema}


def iso():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def inspect(phase):
    work = root / phase
    labels = labels_by_phase[phase]
    rc = int((work / "exit").read_text())
    result = {
        "verified": False, "source": (work / "source").read_text().strip(),
        "exit": rc, "started": (work / "start").read_text().strip(),
        "execution_finished_at": (work / "end").read_text().strip(), "expected": labels,
        "targets": {}, "errors": [],
    }
    # Preserve original exits, even on an unexpected collection failure.
    write(root / (phase + ".json"), result)
    errors = result["errors"]
    if rc:
        errors.append(f"Bazel exited {rc}")
    if result["source"] != (parent if phase == "parent-schema" else src):
        errors.append("wrong source")
    try:
        results = {}
        summaries = {}
        with (work / "bep").open() as events:
            for line in events:
                event = json.loads(line)
                if "started" in event:
                    result["invocation"] = event["started"]["uuid"]
                if "finished" in event:
                    result["bazel_exit_code"] = event["finished"]["exitCode"]["name"]
                if "testResult" in event:
                    label = event["id"]["testResult"]["label"]
                    if label in results:
                        errors.append(f"unexpected second attempt for {label}")
                    results[label] = event["testResult"]
                if "testSummary" in event:
                    summaries[event["id"]["testSummary"]["label"]] = event["testSummary"]["overallStatus"]
        if not result.get("invocation"):
            errors.append("missing original invocation")
        if set(summaries) != set(labels) or set(results) != set(labels):
            errors.append("missing or extra original test results")
        for label in labels:
            status = summaries.get(label)
            original = results.get(label, {})
            execution = original.get("executionInfo", {})
            row = result["targets"][label] = {
                "status": status, "strategy": execution.get("strategy"),
                "cached": bool(original.get("cachedLocally") or execution.get("cachedRemotely")),
            }
            if status != "PASSED" or row["cached"] or row["strategy"] != "remote":
                errors.append(f"missing successful uncached remote execution for {label}")
            log = Path("bazel-testlogs") / label[2:].replace(":", "/") / "test.log"
            if not log.is_file():
                errors.append(f"no test.log for {label}")
                continue
            data = log.read_bytes()
            row["log_sha256"] = hashlib.sha256(data).hexdigest()
            row["log_bytes"] = len(data)
            receipts = [f for f in original.get("testActionOutput", []) if f.get("name") == "test.log"]
            if len(receipts) != 1:
                errors.append(f"missing original log file identity for {label}")
            else:
                log_id = re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)$", receipts[0].get("uri", ""))
                digest = receipts[0].get("digest") or (log_id[1] if log_id else None)
                size = receipts[0].get("length") or (log_id[2] if log_id else None)
                uri = urlparse(receipts[0].get("uri", ""))
                if digest:
                    matches = digest == row["log_sha256"] and (size is None or int(size) == len(data))
                else:
                    matches = uri.scheme == "file" and log.samefile(unquote(uri.path))
                if not matches:
                    errors.append(f"downloaded test.log does not match original for {label}")
            text = data.decode(errors="replace")
            if label in analyzers:
                parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", text)
                sections = dict(zip(parts[1::2], (s.strip() for s in parts[2::2])))
                row["architecture_pass"] = {arch: s == "PASS" for arch, s in sections.items()}
                if len(parts) != 5 or sections != {"amd64": "PASS", "arm64": "PASS"}:
                    errors.append(f"missing or unclean architecture findings for {label}")
            else:
                matches = re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): (\S+)", text)
                row["outcomes"] = dict(Counter(outcome for outcome, _ in matches))
                row["skips"] = [name for outcome, name in matches if outcome == "SKIP"]
                row["failures"] = [name for outcome, name in matches if outcome == "FAIL"]
                if not row["outcomes"].get("PASS") or row["failures"] or not re.search(r"(?m)^PASS\s*$", text):
                    errors.append(f"incomplete/failed Go owner {label}")
                if label in schema:
                    proof = re.search(r"schema-receipt fields=(\d+) cached_field=(true|false) stream=([A-Za-z0-9+/=]+)", text)
                    if not proof:
                        errors.append("no registered-schema check")
                        continue
                    stream = base64.b64decode(proof[3], validate=True)
                    result["schema"] = {"field_count": int(proof[1]), "cached_field": proof[2] == "true",
                                        "stream_sha256": hashlib.sha256(stream).hexdigest(), "stream_bytes": len(stream),
                                        "parent_rejected_before_decode": "parent stream rejected before decoding destination" in text}
                    if phase == "parent-schema":
                        (root / "parent.stream").write_text(proof[3])
                    elif not result["schema"]["parent_rejected_before_decode"]:
                        errors.append("parent stream rejection not observed")
        if result.get("bazel_exit_code") != "SUCCESS":
            errors.append("BEP did not record success")
    except Exception as error:
        errors.append("original evidence incomplete: " + type(error).__name__)
    if rc:
        lines = (work / "log").read_text(errors="replace").splitlines()
        result["diagnostics"] = [line[:400] for line in lines
                                 if re.search(r"(?i)no such target|undefined:|\.go:\d+:\d+:|^FAIL:|no such file or directory|no test targets", line)
                                 and not re.search(r"(?i)credential|remote_header|token|api-key|--|https?://|bearer", line)][:12]
    result["verified"] = not errors
    result["verified_at"] = iso()
    write(root / (phase + ".json"), result)
    print(phase, result["verified"], result.get("invocation"), errors)


if sys.argv[1] == "labels":
    print("\n".join(labels_by_phase[sys.argv[2]]))
elif sys.argv[1] == "inspect":
    inspect(sys.argv[2])
else:
    records = {phase: json.loads((root / (phase + ".json")).read_text()) for phase in labels_by_phase
               if (root / (phase + ".json")).is_file()}
    complete = all(records.get(phase, {}).get("verified") for phase in labels_by_phase)
    if complete:
        old = records["parent-schema"]["schema"]
        new = records["product-schema"]["schema"]
        complete = old["cached_field"] and not new["cached_field"] and old["field_count"] == new["field_count"] + 1
    write(root / "verdict.json", {"verified": complete, "source": src, "parent": parent, "verified_at": iso(),
                                  "phases": {k: v["verified"] for k, v in records.items()}})
    sys.exit(not complete)
