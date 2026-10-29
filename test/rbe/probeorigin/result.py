"""Retain the native wire observations and exact verdict for every physical history."""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sys
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

out = Path(sys.argv[1])
code = int(sys.argv[2])
owner = os.environ["QUALIFICATION_TARGETS"]
dest = out / "original"
dest.mkdir(exist_ok=True)
verdict = {"status": "FAIL", "source": os.environ["QUALIFICATION_COMMIT"], "owner": owner,
           "research": "90fa2602636e3e5d21d81cf0894db2f0e906a859", "reporter": "dab0e0ccc24a86d941dbcde304a443b8aa63d13d", "wrapper_exit": code}
try:
    rows = [json.loads(line) for line in (out / "events.jsonl").open()]
    verdict["invocation"] = next(row["started"]["uuid"] for row in rows if "started" in row)
    verdict["native_exit"] = next(row["finished"]["exitCode"].get("code", 0) for row in rows if "finished" in row)
    [action] = [row["testResult"] for row in rows if row.get("id", {}).get("testResult", {}).get("label") == owner]
    info = action.get("executionInfo", {})
    verdict.update(test_status=action["status"], strategy=info.get("strategy"),
                   cached=bool(action.get("cachedLocally") or info.get("cachedRemotely")),
                   duration_ms=action.get("testAttemptDurationMillis"), start_ms=action.get("testAttemptStartMillisEpoch"))
    verdict["reports"] = {}
    for name in ("test.log", "test.xml"):
        [advertised] = [f for f in action["testActionOutput"] if f["name"] == name]
        path = Path("bazel-testlogs", owner[2:].replace(":", "/"), name)
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        uri = urlparse(advertised["uri"])
        if uri.scheme == "bytestream":
            assert uri.path.endswith(f"/blobs/{sha}/{len(raw)}"), name
        else:
            assert uri.scheme == "file" and Path(unquote(uri.path)).resolve() == path.resolve(), name
        assert advertised.get("digest", sha) == sha and int(advertised.get("length", len(raw))) == len(raw), name
        shutil.copyfile(path, dest / name)
        verdict["reports"][name] = {"bytes": len(raw), "sha256": sha}
    assert verdict["strategy"] == "remote" and not verdict["cached"], "cached or non-remote result"
    cases = ET.parse(dest / "test.xml").findall(".//testcase")
    root = "TestProbeOrigin"
    expected = {root}
    expected.update(f"{root}/{cc}/ts{ts}/expired{expired}/{origin}" for cc in ("reno", "cubic")
                    for ts in ("true", "false") for expired in ("true", "false") for origin in ("original", "probe"))
    names = [case.attrib["name"] for case in cases]
    verdict["census"] = {"expected": len(expected), "actual": len(cases), "missing": sorted(expected - set(names)),
                         "extra": sorted(set(names) - expected)}
    verdict["failed"] = [case.attrib["name"] for case in cases if any(case.find(tag) is not None for tag in ("failure","error","skipped"))]
    assert set(names) == expected and len(names) == len(expected), verdict["census"]
    assert not verdict["failed"] and code == verdict["native_exit"] == 0 and action["status"] == "PASSED"
    trials = {}
    for block in (dest / "test.log").read_text().split("=== RUN   ")[1:]:
        name = block.splitlines()[0].strip()
        if name == root:
            continue
        assert name in expected and name not in trials, name
        _, cc, ts, expired, origin = name.split("/")
        [packet] = re.findall(r"^PROBE ([^\n]+)", block, re.MULTILINE)
        [wire] = re.findall(r"ORIGIN ([^\n]+)", block)
        pr = dict(field.split("=", 1) for field in packet.split())
        # The list of repairs is the final variable-length field.
        first, tail = wire.split(" additional_repairs=")
        row = dict(field.split("=", 1) for field in first.split())
        repairs, timeouts = tail.split(" timeouts=")
        assert (row["cc"], "ts"+row["ts"], "expired"+row["expired"], row["delivered"]) == (cc, ts, expired, origin), name
        assert int(pr["ack"]) == int(row["lastack"]) and int(pr["seq"]) == int(row["tail"]), name
        assert int(pr["end"]) - int(pr["seq"]) == int(row["mss"]), name
        assert int(pr["actual"]) == int(pr["data"]), name
        assert int(pr["actual"]) == (30000000 if expired == "expiredtrue" else 10000000), (name, pr)
        assert 20000000 <= int(pr["measured"]) < 21000000, (name, pr)
        assert (pr["accept"] == "true") == (expired == "expiredfalse"), (name, pr)
        if ts == "tstrue":
            assert int(row["tsecr"]) != 0 and row["original_ts"] != row["probe_ts"], row
        assert timeouts == "0", name
        trials[name] = {"probe": pr, "wire": row, "repairs": repairs, "timeouts": timeouts}
    assert set(trials) == expected-{root}, sorted(trials)
    verdict["trials"] = trials
    verdict["status"] = "PASS"
except (AssertionError, KeyError, OSError, StopIteration, ValueError, ET.ParseError) as exc:
    verdict["error"] = {"type": type(exc).__name__, "detail": str(exc)[:480]}
finally:
    if (out / "lanes.jsonl").is_file():
        shutil.copyfile(out / "lanes.jsonl", dest / "lanes.jsonl")
    (dest / "result.json").write_text(json.dumps(verdict, sort_keys=True, indent=2) + "\n")
sys.exit(verdict["status"] != "PASS")
