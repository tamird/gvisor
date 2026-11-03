"""Compare the two bounded FIFO policies; retain observed outcomes on failure."""

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

BASE = "471d6dff5229b99208bf6fec985601067f1f9d69"
OLD = "ef8ab462943c70e5c52cc57de4b60213f7979fa1"
FIX = "71733cfc7f2c9a7585403f4174b8f82b7b9d2be5"
SOURCE = Path("pkg/tcpip/transport/tcp/dispatcher.go")
FIXTURE = SOURCE.with_name("endpoint_test.go")
TARGET = "//pkg/tcpip/transport/tcp:tcp_test"
FLAGS = ["-c", "opt", "--config=x86_64", "--jobs=4"]
TEMP = Path(os.environ["RUNNER_TEMP"])
OUT = TEMP / "owner"
OUT.mkdir(exist_ok=True)


def git(*args):
    return subprocess.check_output(["git", *args], text=True).rstrip("\n")


def checked(condition, message):
    if not condition:
        raise ValueError(message)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def once(source, old, new):
    checked(source.count(old) == 1, "observer placement changed")
    return source.replace(old, new)


def prepare(owner):
    source = git("show", f"{owner}:{SOURCE}") + "\n"
    clean_digest = hashlib.sha256(source.encode()).hexdigest()
    fields = ("researchVisit func(*Endpoint)", "researchBefore func(*Endpoint)",
              "researchAfter func(*Endpoint)", "researchYield func()")
    source = once(source, "type processor struct {",
                  "type processor struct {\n" + "\n".join(f'{item} `state:"nosave"`' for item in fields))
    for anchor, callback in (
        ("if ep.segmentQueue.empty() {", "if p.researchVisit != nil {p.researchVisit(ep)}"),
        ("if !ep.segmentQueue.empty() && !ep.isOwnedByUser() {", "if p.researchBefore != nil {p.researchBefore(ep)}"),
        ("p.epQ.enqueue(ep)", "if p.researchAfter != nil {p.researchAfter(ep)}"),
        ("runtime.Gosched()", "if p.researchYield != nil {p.researchYield()}"),
    ):
        if anchor == "p.epQ.enqueue(ep)":
            anchor = "\n\t\t\t\t\t" + anchor + "\n"
        if anchor.startswith("if !") or "enqueue" in anchor:
            source = once(source, anchor, anchor + "\n" + callback + "\n")
        else:
            source = once(source, anchor, callback + "\n" + anchor)
    SOURCE.write_text(source)
    return clean_digest, hashlib.sha256(source.encode()).hexdigest()


def observe(owner, debug, source_ids):
    key = f"{owner[:9]}-{debug[-1]}"
    bep = TEMP / f"{key}.bep"
    status = subprocess.run([
        "bazel", "test", *FLAGS, "--strategy=TestRunner=sandboxed", "--nocache_test_results",
        "--test_filter=^TestProcessorFIFOBoundary$", "--test_arg=-test.v",
        f"--test_env=GODEBUG={debug}", "--test_timeout=30", f"--build_event_json_file={bep}", TARGET,
    ]).returncode
    finished = stamp()
    log = Path("bazel-testlogs/pkg/tcpip/transport/tcp/tcp_test/test.log")
    text = log.read_text() if log.exists() else ""
    attempts = []
    if bep.exists():
        for line in bep.read_text().splitlines():
            e = json.loads(line)
            if e["id"].get("testResult", {}).get("label") == TARGET:
                t = e["testResult"]
                info = t.get("executionInfo", {})
                attempts.append(dict(status=t["status"], cached=t.get("cachedLocally", False) or info.get("cachedRemotely", False),
                                     strategy=info.get("strategy"), start=t.get("testAttemptStart"), duration=t.get("testAttemptDuration")))
    rows = []
    for payload in re.findall(r"FIFO_ROW ([^\r\n]+)", text):
        row = dict(item.split("=", 1) for item in payload.split())
        for k in ("batch","resumed","late_resume","misses","batches","yields","stale","a","b","peer","late","cut_ready","cut_late","cut_miss","cut_stale"):
            row[k] = int(row[k])
        for k in ("escape","other","dedup","same","joined","empty","cut_held"):
            checked(row[k] in ("true","false"), "unexpected boolean")
            row[k] = row[k] == "true"
        rows.append(row)
    gates = dict(native_exit=status == 0, fresh=len(attempts) == 1 and not attempts[0]["cached"] and attempts[0]["status"] == "PASSED" and attempts[0]["strategy"] == "processwrapper-sandbox",
                 runtime=text.count(f"FIFO_ENV go=go1.27.3 os=linux arch=amd64 p=1 debug={debug}") == 1,
                 named=text.count("--- PASS: TestProcessorFIFOBoundary (") == 1,
                 cases=[r["kind"] for r in rows] == ["refill","foreign","same","stale","alone"])
    for r in rows:
        kind = r["kind"]
        productive = kind in ("refill","same")
        foreign = kind == "foreign"
        gates[f"work_{kind}"] = (r["batch"] == 100 and r["a"] == 1 and r["b"] == int(foreign)
                                 and r["peer"] == (301 if productive else 0) and r["late"] == int(not foreign)
                                 and r["empty"] and r["other"] == r["dedup"] == foreign
                                 and r["same"] == (kind == "same") and r["joined"] != foreign and r["misses"] >= 1)
        if owner == FIX and r["cut_held"]:
            gates[f"yield_{kind}"] = (r["cut_ready"] == (100 if productive else 0) and r["cut_late"] == 0
                                      and r["cut_miss"] == (2 if kind in ("foreign","same") else 1)
                                      and r["cut_stale"] == int(kind == "stale"))
        if owner == FIX and debug.endswith("1"):
            gates[f"cut_{kind}"] = (r["cut_held"] and not r["escape"] and r["yields"] == 1 and r["late_resume"] == 0
                                    and r["batches"] == int(productive) and r["resumed"] == (100 if productive else 0)
                                    and r["misses"] == (2 if kind in ("foreign","same") else 1)
                                    and r["stale"] == int(kind == "stale"))
        elif debug.endswith("1") and kind in ("refill","foreign","same"):
            gates[f"contrast_{kind}"] = (r["escape"] and r["yields"] == 0 and
                                        ((r["batches"] == 2 and r["resumed"] == 200) if productive else r["misses"] == 6))
    record = dict(product=owner, original_source_sha256=source_ids[0], observed_source_sha256=source_ids[1],
                  setting="cooperative discriminator" if debug.endswith("1") else "ordinary Linux preemption; actual resume is observational",
                  fixture_sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), debug=debug, exit=status,
                  finished=finished, attempts=attempts, rows=rows, gates=gates, verified=stamp(),
                  verdict="MATCHED" if all(gates.values()) else "INCOMPLETE")
    (OUT/f"{key}.json").write_text(json.dumps(record, indent=2)+"\n")
    print(key, record["verdict"], gates, flush=True)
    return all(gates.values())


checked(git("rev-parse", "HEAD^") == FIX and git("rev-parse", f"{FIX}^") == git("rev-parse", f"{OLD}^") == BASE, "unknown product")
checked(git("diff", "--name-only", OLD, FIX) == str(SOURCE), "unexpected arm difference")
verdicts = []
for owner in (OLD, FIX):
    ids = prepare(owner)
    for debug in ("asyncpreemptoff=1", "asyncpreemptoff=0"):
        verdicts.append(observe(owner, debug, ids))
raise SystemExit(0 if all(verdicts) else 1)
