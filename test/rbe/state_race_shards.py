"""Complete the original timed-out race owner with unchanged per-shard limits."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

KEY = os.environ.get("BUILDBUDDY_API_KEY", "")
ROOT = Path(os.environ["RUNNER_TEMP"]) / "state-race-shards"
OUT = ROOT / "evidence"
OUT.mkdir(parents=True, exist_ok=True)
SOURCE = "255bc204e6d5304fb4816605c2b32aaf1b311000"
OWNER = "//pkg/state/tests:tests_test"
ORIGINAL = "ee6cc244-4e7d-447b-8283-389dfdcaab85"
ORIGINAL_SOURCE = "4cebf830f4b7128ff5ed8f0e24401bbeb1b7980b"
SHARDS = 4

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def request(method, body, raw=False):
    path = "/rpc/BuildBuddyService/SearchInvocation" if method == "SearchInvocation" else "/api/v1/" + method
    req = urllib.request.Request(
        "https://app.buildbuddy.io" + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-buildbuddy-api-key": KEY},
    )
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=60) as response:
        content = response.read(32 * 1024 * 1024 + 1)
    assert len(content) <= 32 * 1024 * 1024
    return content if raw else json.loads(content)

def pages(method, body, field):
    result = []
    for _ in range(5):
        value = request(method, body)
        result.extend(value.get(field, []))
        token = value.get("nextPageToken", value.get("next_page_token"))
        if not token:
            return result
        body = dict(body, page_token=token)
    raise ValueError("Invocation metadata exceeded bounded pagination")

def value(item, camel, snake):
    return item.get(camel, item.get(snake))

def identifier(item):
    return str(uuid.UUID(value(item.get("id", item), "invocationId", "invocation_id")))

def write(name, obj):
    data = (json.dumps(obj, indent=2) + "\n").encode()
    assert not KEY or KEY.encode() not in data
    (OUT / name).write_bytes(data)


def cases(raw):
    result = {}
    for case in ET.fromstring(raw).iter("testcase"):
        key = json.dumps([case.get("classname"), case.get("name")], separators=(",", ":"))
        assert key not in result, "Duplicate case in an XML report"
        disposition = "FAIL" if any(case.find(k) is not None for k in ("failure", "error")) else "SKIP" if case.find("skipped") is not None or case.get("status") in ("notrun", "suppressed") or case.get("result") == "suppressed" else "PASS"
        result[key] = disposition
    assert result
    return result


def census_digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def expected():
    assert KEY
    write("expected.json", {"retrieval": "INCOMPLETE", "source": ORIGINAL_SOURCE, "invocation": ORIGINAL, "owner": OWNER})
    row, = pages("GetTarget", {"selector": {"invocation_id": ORIGINAL, "label": OWNER}}, "target")
    assert identifier(row) == ORIGINAL and row["label"] == OWNER and row["status"] == "PASSED"
    actions = pages("GetAction", {"selector": {"invocation_id": ORIGINAL, "target_label": OWNER}}, "action")
    files = []
    for action in actions:
        assert identifier(action) == ORIGINAL and value(action, "targetLabel", "target_label") == OWNER
        files.extend(f for f in action.get("file", []) if f.get("name") == "test.xml")
    file, = files
    uri = file["uri"]
    match = re.fullmatch(r"bytestream://(?:remote|app)\.buildbuddy\.io/(?:[^?#\s]+/)?blobs/([0-9a-f]{64})/(\d+)", uri)
    assert match
    raw = request("GetFile", {"uri": uri}, raw=True)
    assert len(raw) == int(match[2]) and hashlib.sha256(raw).hexdigest() == match[1]
    rows = cases(raw)
    assert len(rows) == 11561 and list(rows.values()).count("SKIP") == 85 and "FAIL" not in rows.values()
    (ROOT / "expected.json").write_text(json.dumps(rows))
    write("expected.json", {"retrieval": "RECOVERED", "source": ORIGINAL_SOURCE, "invocation": ORIGINAL, "owner": OWNER,
                            "xml_sha256": match[1], "xml_bytes": len(raw), "case_count": len(rows),
                            "skips": 85, "case_census_sha256": census_digest(rows)})


def run():
    result = {"status": "INCOMPLETE", "source": SOURCE, "owner": OWNER, "expected_shards": SHARDS,
              "per_shard_timeout_seconds": 60, "original_race_status": "TIMED_OUT",
              "original_race_invocation": "13a6a3a3-9897-461a-b859-58c916db2c77", "shards": []}
    write("verdict.json", result)
    try:
        git = ["git", "-c", "safe.directory=" + str(Path.cwd())]
        assert subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip() == SOURCE
        assert not subprocess.check_output(git + ["diff", "HEAD", "--"])
        result["native_architecture"] = os.uname().machine
        assert result["native_architecture"] == "x86_64"
        expected_cases = json.loads((ROOT / "expected.json").read_text())
        bep = ROOT / "private-bep.json"
        args = ["bazel", "--bazelrc=" + os.environ["RUNNER_TEMP"] + "/tcp.bazelrc", "test",
                "--config=x86_64", "--config=race", "--jobs=400", "--spawn_strategy=remote,local",
                "--noremote_upload_local_results", "--strategy=GoStaticAnalysis=local",
                "--strategy=GoStandardLibraryAnalysis=local", "--strategy=TestRunner=local",
                "--remote_download_outputs=toplevel", "--nocache_test_results",
                "--test_sharding_strategy=forced=4", "--test_timeout=60", "--test_arg=-test.v",
                "--build_event_json_file=" + str(bep), "--build_event_json_file_path_conversion=false", OWNER]
        result["started_at_ns"] = time.time_ns()
        with (ROOT / "private-command.log").open("wb") as log:
            code = subprocess.run(args, stdout=log, stderr=log).returncode
        result.update(native_exit=code, execution_finished_at_ns=time.time_ns(), status="FAIL" if code else "INCOMPLETE")
        result["invocations"] = re.findall(r"Invocation ID: ([0-9a-f-]{36})", (ROOT / "private-command.log").read_text(errors="replace"))
        write("verdict.json", result)
        events = [json.loads(line) for line in bep.read_text().splitlines() if line.strip()]
        finished, = [e["finished"] for e in events if "finished" in e]
        assert int(finished["exitCode"].get("code", 0)) == code
        tests = [e for e in events if "testResult" in e.get("id", {})]
        assert len(tests) == SHARDS and {int(e["id"]["testResult"]["shard"]) for e in tests} == set(range(1, SHARDS + 1))
        configurations = {e["id"]["testResult"]["configuration"]["id"] for e in tests}
        assert len(configurations) == 1 and all(configurations)
        result["configuration_id"] = next(iter(configurations))
        summaries = [e for e in events if "testSummary" in e.get("id", {})]
        assert len(summaries) == 1 and summaries[0]["id"]["testSummary"]["label"] == OWNER
        result["summary"] = {k: summaries[0]["testSummary"].get(k) for k in ("overallStatus", "totalRunCount")}
        observed = {}
        for event in tests:
            identity = event["id"]["testResult"]
            original = event["testResult"]
            assert identity["label"] == OWNER and int(identity.get("run", 1)) == 1 and int(identity.get("attempt", 1)) == 1
            shard = int(identity["shard"])
            row = {"shard": shard, "original_status": original["status"],
                   "cached_locally": original.get("cachedLocally", False),
                   "strategy": original.get("executionInfo", {}).get("strategy"),
                   "cached_remotely": original.get("executionInfo", {}).get("cachedRemotely", False)}
            result["shards"].append(row)
            write("verdict.json", result)
            assert not row["cached_locally"] and not row["cached_remotely"] and row["strategy"] == "local"
            file, = [f for f in original.get("testActionOutput", []) if f.get("name") == "test.xml"]
            uri = urllib.parse.urlparse(file["uri"])
            assert uri.scheme == "file" and not uri.netloc
            raw = Path(urllib.parse.unquote(uri.path)).read_bytes()
            found = cases(raw)
            assert not observed.keys() & found.keys(), "A test case ran in multiple shards"
            observed.update(found)
            row.update(cases=len(found), passed=list(found.values()).count("PASS"), skipped=list(found.values()).count("SKIP"), failed=list(found.values()).count("FAIL"), xml_sha256=hashlib.sha256(raw).hexdigest())
            if row["failed"]:
                result["status"] = "FAIL"
            (OUT / f"shard-{shard - 1}.xml").write_bytes(raw)
            write("verdict.json", result)
        missing = sorted(expected_cases.keys() - observed.keys())
        extra = sorted(observed.keys() - expected_cases.keys())
        changed = sorted(k for k in expected_cases.keys() & observed.keys() if expected_cases[k] != observed[k])
        result.update(expected_cases=len(expected_cases), observed_cases=len(observed), missing_cases=missing,
                      extra_cases=extra, changed_dispositions=changed, expected_census_sha256=census_digest(expected_cases),
                      observed_census_sha256=census_digest(observed))
        assert not subprocess.check_output(git + ["diff", "HEAD", "--"])
        if not code and all(r["original_status"] == "PASSED" for r in result["shards"]) and result["summary"]["overallStatus"] == "PASSED" and int(result["summary"]["totalRunCount"]) == SHARDS and not missing and not extra and not changed:
            result["status"] = "PASS"
    except Exception as err:
        result["error_type"] = type(err).__name__
        result["error_line"] = traceback.extract_tb(err.__traceback__)[-1].lineno
    result["verified_at_ns"] = time.time_ns()
    write("verdict.json", result)
    print(result["status"], result.get("native_exit"), len(result["shards"]))
    return result["status"] != "PASS"


if __name__ == "__main__":
    if sys.argv[1] == "expected":
        try:
            expected()
        except Exception as err:
            write("expected.json", {"retrieval": "INCOMPLETE", "source": ORIGINAL_SOURCE, "invocation": ORIGINAL, "owner": OWNER, "error_type": type(err).__name__, "error_line": traceback.extract_tb(err.__traceback__)[-1].lineno})
            raise SystemExit(1)
    else:
        raise SystemExit(run())
