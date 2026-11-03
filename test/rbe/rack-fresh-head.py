"""Check the source, fresh remote attempts, and executed leaves of the two DUTs."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from xml.etree import ElementTree

from qualification_report import xml_cases

ROOT = Path(os.environ["RUNNER_TEMP"])
RECORDS = ROOT / "records"
TOP = "TestRACKFreshTimeoutRetransmission"
FIXTURE = "98aeb3e61b3657dae3bc069aa701d1e037c277a8"
LABELS = ["//test/packetimpact/tests:tcp_rack_" + dut + "_test" for dut in ("native", "netstack")]



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


def field(name):
    path = RECORDS / name
    return path.read_text().strip() if path.is_file() else None


def inspect(directory, label, events):
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
    log = directory / (target + ".test.log")
    xml = directory / (target + ".test.xml")
    row = {"attempts": attempts, "summaries": summaries, "pass": False}
    if not (log.is_file() and xml.is_file() and xml.stat().st_size):
        return row
    data = log.read_bytes()
    row.update(log_sha256=hashlib.sha256(data).hexdigest(), xml_sha256=hashlib.sha256(xml.read_bytes()).hexdigest())
    message = data.decode(errors="replace")
    tree = ElementTree.parse(xml)
    names = ["/".join(filter(None, (case.get("classname"), case.get("name")))) for case in tree.iter("testcase")]
    matches = [name for name in names if name == TOP or name.endswith("/" + TOP)]
    counts = xml_cases(xml, frozenset(matches))
    row["xml"] = {k: counts[k] for k in ("passed", "failed", "skipped")}
    row["named"] = len(matches) == 1
    # Some wrappers leave successful verbose output in XML instead of test.log.
    # Read one source per observation type so duplicated output cannot inflate counts.
    xml_text = "\n".join(tree.getroot().itertext())
    def observations(pattern):
        return re.findall(pattern, message) or re.findall(pattern, xml_text)
    repairs = [list(map(int, groups)) for groups in observations(r"post-SACK repair: offset=(\d+) length=(\d+) MSS=(\d+)")]
    row["observed_offset_length_mss"] = repairs
    outside = observations(r"repair \[(\d+), (\d+)\) outside the holes \[(\d+), (\d+)\)")
    row["outside_holes"] = [list(map(int, groups)) for groups in outside]
    inside = all(mss > 0 and n > 0 and mss <= off and off + n <= 3*mss for off, n, mss in repairs)
    fresh = len(attempts) == 1 and attempts[0]["strategy"] == "remote" and not attempts[0]["cached"]
    row["pass"] = (fresh and row["named"] and summaries == ["PASSED"] and attempts[0]["status"] == "PASSED" and
                   counts["failed"] == counts["skipped"] == 0 and matches[0] in counts.get("required_passed", [])
                   and inside and not outside)
    row["rejected_tail"] = (fresh and row["named"] and summaries == ["FAILED"] and attempts[0]["status"] == "FAILED" and
                            counts["failed"] == 1 and counts["skipped"] == 0 and matches[0] in counts["failures"] and
                            bool(outside) and any(mss > 0 and n > 0 and off == 6*mss for off, n, mss in repairs))
    return row


def phase(name, labels, source, rack):
    directory = RECORDS / name
    row = {key: field(name + "/" + key) for key in ("source", "fixture", "rack-blob", "invocation", "finished", "exit")}
    row["source_bound"] = row["source"] == source and row["fixture"] == FIXTURE and row["rack-blob"] == rack
    row["owners"] = {}
    events = []
    try:
        with (directory / "packet.bep").open() as stream:
            for line in stream:
                entry = json.loads(line)
                if set(entry.get("id", {})) & {"targetCompleted", "testResult", "testSummary"}:
                    events.append(entry)
    except (OSError, ValueError) as exc:
        row["event_error"] = type(exc).__name__
    for label in labels:
        try:
            row["owners"][label] = inspect(directory, label, events)
        except (OSError, ValueError, ElementTree.ParseError) as exc:
            row["owners"][label] = {"pass": False, "error": type(exc).__name__}
    log = directory / "packet.log"
    if log.is_file():
        data = log.read_bytes()
        row["bazel_log_sha256"] = hashlib.sha256(data).hexdigest()
        text = data.decode(errors="replace")
        row["compiler"] = compiler_diagnostic(text)
        row["diagnostic_classes"] = [term for term in ("not a recognized option", "no such package",
            "error downloading", "no matching toolchains", "no usable", "exec format error",
            "deadline exceeded", "failed to fetch", "permission denied", "undefined:", "panic:") if term in text.lower()]
    return row


def main():
    fixed = phase("candidate", LABELS, os.environ["PRODUCT"], "07f593bf7086e0f4146885f2f74fb0dde86b635b")
    old = phase("published", LABELS[1:], os.environ["BASE"], "75ebbf576ad7136123c5835323ed225901b5e16a")
    doc = {"carrier": os.environ["GITHUB_SHA"], "clock_run": 41711895058, "prior_zero_test_run": 41703606850,
           "bazel": field("bazel"), "finished": field("finished"), "candidate": fixed, "published": old}
    fixed_ok = fixed["source_bound"] and fixed["exit"] == "0" and all(r["pass"] for r in fixed["owners"].values())
    old_result = old["owners"][LABELS[1]]
    if old["source_bound"] and old["exit"] == "3" and old_result.get("rejected_tail"):
        result = "PUBLISHED_REPLAYED_SEGMENT_7"
    elif old["source_bound"] and old["exit"] == "0" and old_result["pass"]:
        result = "PUBLISHED_ALSO_PASSED_THIS_PACKET_WINDOW"
    else:
        result = "PUBLISHED_CONTROL_UNCLASSIFIED"
    doc.update(published_outcome=result, verified=bool(fixed_ok and result != "PUBLISHED_CONTROL_UNCLASSIFIED"),
               verified_at=datetime.now(timezone.utc).isoformat())
    (RECORDS / "verdict.json").write_text(json.dumps(doc, indent=2) + "\n")
    return not doc["verified"]


if __name__ == "__main__":
    raise SystemExit(main())
