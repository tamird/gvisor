"""Retain only the named TCP test results, measured events and report identities."""
import hashlib
import json
from urllib.parse import unquote, urlparse
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from matched_test_result import read_lanes
from tail_probe_observation import observe

out, wrapper_exit = Path(sys.argv[1]), int(sys.argv[2])
owner = os.environ["QUALIFICATION_TARGETS"]
suite = os.environ["QUALIFICATION_TEST_FILTER"].strip("^$")
stock, fixed, parent = "54dd721a4650c8c5f6afb72dfcc590442b4a13a0", "4c31b4fc4c319eb6aa71cda9bc1b8365739c20d6", "26a3cc72d469db14c5cdf7ddcf101fe1ac71592d"
product = os.environ.get("QUALIFICATION_PRODUCT", os.environ["QUALIFICATION_COMMIT"])
legacy_stock = "949510d08e9ce3e8cccb3fd7fc1d7f3348a057cd"
legacy_fixed = "81e3444fede09fe593134603cc0facbf231e4cb3"
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
result = {"status": "FAIL", "source": git("rev-parse", "HEAD"), "product": product, "owner": owner, "wrapper_exit": wrapper_exit}
stage = "source"
try:
    assert result["source"] == os.environ["QUALIFICATION_COMMIT"]
    if suite == "TestGSOTailLossProbe":
        assert result["source"] == product and product in {stock, fixed} and git("rev-parse", "HEAD^") == parent
        assert git("diff", "--name-only", stock, fixed).splitlines() == ["pkg/tcpip/transport/tcp/rack.go"]
        assert git("diff", "--name-only", parent, stock).splitlines() == ["pkg/tcpip/transport/tcp/test/e2e/tcp_rack_test.go"]
    else:
        assert suite == "TestTailProbeLastPacket" and product in {legacy_stock, legacy_fixed}
        assert git("rev-parse", product + "^") == (legacy_stock if product == legacy_fixed else "03e72471a6e61beecec932f838a991a1585eae50")
        subprocess.run(["git", "merge-base", "--is-ancestor", product, "HEAD"], check=True)
        allowed = {".github/workflows/build.yml", *(f"test/rbe/{name}" for name in (
            "qualify.sh", "unit_matrix.py", "first_loss_result.py", "matched_test_result.py", "tail_probe_observation.py"))}
        assert set(git("diff", "--name-only", product, "HEAD").splitlines()) <= allowed
    assert not git("status", "--porcelain", "--untracked-files=no")
    rows = [json.loads(line) for line in (out / "events.jsonl").open()]
    stage = "BEP"
    attempts = [r for r in rows if r.get("id", {}).get("testResult", {}).get("label") == owner]
    result["attempts"] = len(attempts)
    result["failure_categories"] = sorted({reason if re.fullmatch(r"[A-Z_]{1,40}", reason) else "UNSPECIFIED" for r in rows if "aborted" in r for reason in [r["aborted"].get("reason", "UNSPECIFIED")]})
    assert len(attempts) == 1
    test = attempts[0]["testResult"]
    result["remote"] = test.get("executionInfo", {}).get("strategy") == "remote"
    result["cached"] = bool(test.get("cachedLocally") or test.get("executionInfo", {}).get("cachedRemotely"))
    result["child"] = next((r["started"].get("uuid") for r in rows if "started" in r), None)
    assert result["remote"] and not result["cached"]
    result["test_status"] = test["status"]
    stage = "reports"
    files = {f["name"]: f for f in test["testActionOutput"] if f["name"] in {"test.log", "test.xml"}}
    assert set(files) == {"test.log", "test.xml"} and len([f for f in test["testActionOutput"] if f["name"] in files]) == 2
    content = {}
    result["reports"] = {}
    for name, f in files.items():
        file = Path("bazel-testlogs",owner[2:].replace(":", "/"),name)
        data = file.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        uri = urlparse(f["uri"])
        if uri.scheme == "bytestream":
            assert uri.path.endswith(f"/blobs/{digest}/{len(data)}")
        else:
            assert uri.scheme == "file" and file.resolve() == Path(unquote(uri.path)).resolve()
        if f.get("digest"):
            assert f["digest"] == digest
        if "length" in f:
            assert int(f["length"]) == len(data)
        content[name] = data
        result["reports"][name] = {"sha256": digest, "bytes": len(data)}
    stage = "named cases"
    lanes = read_lanes(out)
    result["lanes"] = lanes
    result.update(observe(suite, product in {stock, legacy_stock}, wrapper_exit, lanes, test["status"],
                          content["test.xml"], content["test.log"].decode(), result["failure_categories"]))
    result["status"] = "PASS"
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as err:
    result["error"] = {"stage": stage, "kind": type(err).__name__, "detail": str(err)[:180]}
finally:
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
sys.exit(int(result["status"]!="PASS"))
