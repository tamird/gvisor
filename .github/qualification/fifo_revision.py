"""Record only source-bound verdicts for the six FIFO and pipe owners."""
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlparse
import uuid
import xml.etree.ElementTree as ET


SOURCE = "d6786915fa60f2dc04fc6969fc8ebf3274b39328"
FIFO = ["MknodAtFIFO", "Fifo", "HangupDependsOnWhenReaderOpened", "FifoOtrunc",
        "FifoTruncNoOp", "OpenBlockedAndInterrupted", "FifoOpenRDWR"]
PIPE = ["TestPipeRW", "TestPipeReadBlock", "TestPipeWriteBlock", "TestPipeWriteUntilEnd"]
TARGETS = {"//test/syscalls:fifo_test_" + flavor: FIFO
           for flavor in ("native", "runsc_systrap_directfs", "runsc_systrap_shared")}
TARGETS["//pkg/sentry/kernel/pipe:pipe_test"] = PIPE
for owner in ("pipe_nogo", "pipe_test_nogo"):
    TARGETS["//pkg/sentry/kernel/pipe:" + owner] = []
HEX = re.compile(r"[0-9a-f]{64}")
TERMS = ("permission denied", "no such file or directory", "action conflict", "no registered executors",
         "no matching execution platform", "operation not permitted", "resource exhausted",
         "deadline exceeded", "analysis of target", "signal: killed", "not declared in package",
         "segmentation fault", "no tests were found", "unrecognized option", "not a valid test")


def signals(text):
    return [s for s in TERMS if s in text.lower()]


def original(label, entry):
    name = entry["name"]
    uri = urlparse(entry.get("uri", ""))
    ref = re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/([0-9]+)$", uri.path)
    expected = (ref[1], int(ref[2])) if ref else (entry.get("digest"), int(entry.get("length", 0)))
    subpath = label[2:].replace(":", "/") + "/" + name
    paths = [Path(unquote(uri.path))] if uri.scheme == "file" else []
    paths.extend(Path("bazel-out").glob("*/testlogs/" + subpath))
    for path in paths:
        if not path.is_file() or path.stat().st_size > 5 * 1024 ** 2:
            continue
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if expected[0] is not None and (sha, len(raw)) != expected:
            continue
        return raw, {"sha256": sha, "bytes": len(raw), "matchesBEP": expected == (sha, len(raw))}
    return None, None


def decode_xml(raw, expected):
    reported = {}
    unknown = 0
    for case in ET.fromstring(raw).iter("testcase"):
        name = case.get("name", "")
        suffix = re.split(r"[./]", name)[-1]
        if suffix not in expected or (expected == FIFO and "FifoTest" not in case.get("classname", "") + name):
            unknown += 1
            continue
        if case.find("failure") is not None or case.find("error") is not None:
            verdict = "failed"
        elif case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") in ("skipped", "suppressed"):
            verdict = "skipped"
        else:
            verdict = "passed"
        reported.setdefault(suffix, []).append(verdict)
    return reported, unknown


def collect(work):
    evidence = {"source": SOURCE, "verified": False, "owners": {t: [] for t in TARGETS},
                "failures": [], "skips": []}
    exit_file = work / "exit"
    evidence["bazelExit"] = int(exit_file.read_text()) if exit_file.exists() else None
    log = work / "console"
    evidence["signals"] = signals(log.read_text(errors="replace")) if log.exists() else []
    bep = work / "events"
    failures = evidence["failures"]
    if bep.exists():
        with bep.open() as stream:
            for line in stream:
                event = json.loads(line)
                ident = event["id"]
                if "started" in event:
                    evidence["invocation"] = str(uuid.UUID(event["started"]["uuid"]))
                if "finished" in event:
                    evidence["bepExit"] = int(event["finished"].get("exitCode", {}).get("code", 0))
                if "action" in event and not event["action"].get("success") and len(failures) < 10:
                    action = event["action"]
                    mnemonic = action.get("type", "")
                    uri = action.get("stderr", {}).get("uri", "")
                    ref = re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$", uri)
                    label = ident.get("actionCompleted", {}).get("label", "")
                    failures.append({"mnemonic": mnemonic if re.fullmatch(r"[A-Za-z0-9_]{1,70}", mnemonic) else "unknown",
                                     "owner": label if re.fullmatch(r"//[\w+./-]*:[\w+./-]+", label) else "dependency",
                                     "stderr": [ref[1], int(ref[2])] if ref else None})
                label = ident.get("testResult", {}).get("label")
                if label not in TARGETS or "testResult" not in event:
                    continue
                body = event["testResult"]
                info = body.get("executionInfo", {})
                status = body.get("status", "")
                row = {"status": status if re.fullmatch("[A-Z_]{1,32}", status) else "unknown",
                       "remote": info.get("strategy") == "remote",
                       "cached": bool(body.get("cachedLocally") or info.get("cachedRemotely"))}
                evidence["owners"][label].append(row)
                expected = TARGETS[label]
                for entry in body.get("testActionOutput", []):
                    name = entry["name"]
                    if name not in ("test.xml", "test.log"):
                        continue
                    raw, identity = original(label, entry)
                    if not raw:
                        continue
                    row[name] = identity
                    if name == "test.xml" and expected:
                        try:
                            row["cases"], row["otherCases"] = decode_xml(raw, expected)
                        except ET.ParseError:
                            row["invalidXML"] = True
                    if name == "test.log":
                        text = raw.decode(errors="replace")
                        row["signals"] = signals(text)
                        if not expected:
                            row["architectures"] = dict(re.findall(r"(amd64|arm64)\.\.\.[ \t]*(PASS|FAIL)\b", text))
                        elif expected == FIFO:
                            row["logCases"] = {case: verdict for verdict, case in re.findall(
                                r"(?m)^\[\s*(OK|FAILED|SKIPPED)\s*\]\s+FifoTest\.([A-Za-z0-9_]+)\b", text)
                                if case in FIFO}
                        else:
                            row["logCases"] = {case: verdict for verdict, case in re.findall(
                                r"(?m)^\s*--- (PASS|FAIL|SKIP): (Test[A-Za-z0-9_]+)\b", text) if case in PIPE}
                if expected:
                    for n, results in row.get("cases", {}).items():
                        if "skipped" in results:
                            evidence["skips"].append([label, n])
                    covered = row.get("cases") == {n: ["passed"] for n in expected} and row.get("otherCases") == 0
                else:
                    covered = row.get("architectures") == {"amd64": "PASS", "arm64": "PASS"}
                row["verified"] = bool(covered and row["status"] == "PASSED" and row["remote"] and not row["cached"])
    evidence["verified"] = (evidence.get("bepExit") == evidence["bazelExit"] == 0 and not failures
                            and all(len(rows) == 1 and rows[0]["verified"] for rows in evidence["owners"].values()))
    (work / "result.json").write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
    return 0 if evidence["verified"] else 1


if __name__ == "__main__":
    sys.exit(collect(Path(sys.argv[1])))
