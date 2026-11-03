"""Check the source, fresh remote attempts, and executed leaves of the two DUTs."""

from datetime import datetime, timezone
import hashlib
from http.client import HTTPException, HTTPSConnection
import json
import os
from pathlib import Path
import re
import sys
from xml.etree import ElementTree

from qualification_report import xml_cases

ROOT = Path(os.environ["RUNNER_TEMP"])
RECORDS = ROOT / "records"
TOP = "TestRTOAfterPartialACK"
LEAVES = [TOP + "/" + leaf for leaf in ("no_ack", "half_segment")]
LABELS = ["//test/packetimpact/tests:tcp_rack_" + dut + "_test" for dut in ("native", "netstack")]
PRIOR = "93a5fd1d-ad90-4b39-bdb1-a10ca143635e"


def compiler_diagnostic(text):
    """Emit code locations and fixed error categories, never Bazel argv or arbitrary logs."""
    categories = {
        "killed compiler": r"killed signal terminated|out of memory|cannot allocate memory",
        "missing standard header": r"(?:cstdlib|stdlib\.h|stddef\.h|cstdint|stdint\.h|features\.h|iostream): no such file",
        "missing compile input": r"fatal error: .*no such file or directory",
        "compiler executable missing": r"cannot execute .*cc1plus|error trying to exec .*cc1plus",
        "not declared": r"not (?:been )?declared|undeclared",
        "missing type or member": r"is not a member of|does not name a type|has no member named",
        "compiler option rejected": r"unrecognized command.line option|unknown argument|not supported",
        "no matching call": r"no matching function|no viable conversion",
        "disk exhausted": r"no space left on device",
        "assembler failed": r"assembler messages:|error: no such instruction",
    }
    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    entries = []
    for line in clean.splitlines():
        line = line[:8192]
        if not re.search(r"\b(?:fatal error|error):|killed signal terminated", line, re.I):
            continue
        if line.startswith("ERROR:") or "error executing CppCompile command" in line or " failed: (Exit " in line:
            continue
        entry = {"category": next((key for key, value in categories.items() if re.search(value, line, re.I)),
                                  "unclassified compiler diagnostic")}
        location = re.search(r"(?<![A-Za-z0-9_.-])(bcm[.]cc|generator[.]cc|arena[.]cc|cstdlib|stdlib[.]h|stddef[.]h|stdint[.]h|features[.]h):(\d{1,7})(?::(\d{1,5}))?", line)
        if location:
            entry["file"], entry["line"], entry["column"] = location.groups()
        if warning := re.search(r"\[-W(?:error=)?(sign-compare|unused-parameter|array-bounds|deprecated-declarations|maybe-uninitialized)\]", line):
            entry["warning"] = warning[1]
        entries.append(entry)
        if len(entries) == 8:
            break
    return entries


def original():
    row = {"invocation": PRIOR, "source": "41703606850", "pages": 0}
    try:
        token = "0"
        text = ""
        for _ in range(6):
            conn = HTTPSConnection("app.buildbuddy.io", timeout=12)
            try:
                conn.request("POST", "/api/v1/GetLog", json.dumps({"selector": {"invocation_id": PRIOR},
                    "page_token": token}), {"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
                response = conn.getresponse()
                row["http_status"] = response.status
                if response.status != 200:
                    break
                raw = response.read(3 * 1024**2 + 1)
                if len(raw) > 3 * 1024**2:
                    break
                page = json.loads(raw)
            finally:
                conn.close()
            row["pages"] += 1
            text += page.get("log", {}).get("contents", "")
            token = page.get("next_page_token") or page.get("nextPageToken")
            if not token or len(text) > 8 * 1024**2:
                break
        row["diagnostics"] = compiler_diagnostic(text)
    except (HTTPException, OSError, ValueError) as exc:
        row["error_type"] = type(exc).__name__
    (RECORDS / "original.json").write_text(json.dumps(row))


def field(name):
    path = RECORDS / name
    return path.read_text().strip() if path.is_file() else None


def inspect(label, events):
    target = label.split(":")[1]
    related = {label}
    for entry in events:
        if entry["id"].get("targetCompleted", {}).get("label") == label:
            for child in entry.get("children", []):
                for kind in ("targetCompleted", "testResult", "testSummary"):
                    if other := child.get(kind, {}).get("label"):
                        related.add(other)
    attempts, summaries = [], []
    for entry in events:
        if entry["id"].get("testResult", {}).get("label") in related:
            result = entry["testResult"]
            ident = entry["id"]["testResult"]
            execution = result.get("executionInfo", {})
            attempts.append({"label": ident["label"], "configuration": ident.get("configuration", {}).get("id"),
                             "run": ident.get("run"), "shard": ident.get("shard"), "attempt": ident.get("attempt"),
                             "strategy": execution.get("strategy"), "status": result["status"],
                             "cached": bool(result.get("cachedLocally") or execution.get("cachedRemotely")),
                             "start": result.get("testAttemptStart") or result.get("testAttemptStartMillisEpoch"),
                             "duration": result.get("testAttemptDuration") or result.get("testAttemptDurationMillis")})
        if entry["id"].get("testSummary", {}).get("label") in related:
            summaries.append(entry["testSummary"]["overallStatus"])
    log = ROOT / (target + ".test.log")
    xml = ROOT / (target + ".test.xml")
    row = {"attempts": attempts, "summaries": summaries, "pass": False}
    if not (log.is_file() and xml.is_file() and xml.stat().st_size):
        return row
    data = log.read_bytes()
    row.update(log_sha256=hashlib.sha256(data).hexdigest(), xml_sha256=hashlib.sha256(xml.read_bytes()).hexdigest())
    message = data.decode(errors="replace")
    actual = ["/".join(filter(None, (case.get("classname"), case.get("name"))))
              for case in ElementTree.parse(xml).iter("testcase")]
    names = {leaf: [name for name in actual if name == leaf or name.endswith("/" + leaf)] for leaf in LEAVES}
    required = frozenset(name for values in names.values() for name in values)
    counts = xml_cases(xml, required)
    row["xml_counts"] = {key: counts[key] for key in ("passed", "failed", "skipped")}
    row["leaves"] = {}
    for leaf, matches in names.items():
        printed = re.findall(r"(?:^|\s)--- (PASS|FAIL|SKIP): " + re.escape(leaf) + r" \(", message, re.MULTILINE)
        row["leaves"][leaf] = {"xml": len(matches) == 1 and matches[0] in counts.get("required_passed", []),
                               "log": printed}
    row["pass"] = (summaries == ["PASSED"] and len(attempts) == 1 and attempts[0]["status"] == "PASSED"
                   and attempts[0]["strategy"] == "remote" and not attempts[0]["cached"]
                   and counts["failed"] == 0 and counts["skipped"] == 0 and all(
                       (v["xml"] or v["log"] == ["PASS"]) and not (set(v["log"]) - {"PASS"})
                       for v in row["leaves"].values()))
    return row


def main():
    doc = {"source": os.environ["PRODUCT"], "carrier": os.environ["GITHUB_SHA"], "invocation": field("invocation"),
           "fixture": field("packet-blob"), "native_exit": field("exit"), "finished": field("finished"),
           "earlier_measurements": "https://app.buildbuddy.io/invocation/41a38860-e1bf-4a91-8278-ea68ef60b656",
           "packetimpact_nogo": "disabled by packetimpact_testbench", "owners": {}, "verified": False}
    doc["original_cppcompile"] = json.loads(field("original.json") or "null")
    doc["bazel"] = field("bazel")
    events = []
    bep = ROOT / "packet.bep"
    if bep.is_file():
        with bep.open() as stream:
            for line in stream:
                entry = json.loads(line)
                if set(entry.get("id", {})) & {"targetCompleted", "testResult", "testSummary"}:
                    events.append(entry)
    for label in LABELS:
        try:
            doc["owners"][label] = inspect(label, events)
        except (OSError, ValueError, ElementTree.ParseError) as exc:
            doc["owners"][label] = {"pass": False, "error": type(exc).__name__}
    log = ROOT / "packet.log"
    if log.is_file():
        data = log.read_bytes()
        doc["bazel_log_sha256"] = hashlib.sha256(data).hexdigest()
        text = data.decode(errors="replace").lower()
        doc["compiler"] = compiler_diagnostic(data.decode(errors="replace"))
        doc["diagnostic_classes"] = [term for term in ("not a recognized option", "no such package",
            "error downloading", "no matching toolchains", "no usable", "exec format error",
            "deadline exceeded", "failed to fetch", "permission denied", "undefined:", "panic:") if term in text]
    doc["verified"] = (doc["fixture"] == "673f8d21dfc7ab78db7b05448a5561672206bd42" and doc["native_exit"] == "0"
                       and all(row["pass"] for row in doc["owners"].values()))
    doc["verified_at"] = datetime.now(timezone.utc).isoformat()
    (RECORDS / "verdict.json").write_text(json.dumps(doc, indent=2) + "\n")
    return not doc["verified"]


if __name__ == "__main__":
    if sys.argv[1:] == ["--original"]:
        original()
        raise SystemExit(0)
    raise SystemExit(main())
