"""Bind the original test report to one uncached, native Linux action."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

from image_execution import execution_records

result = Path(os.environ["RUNNER_TEMP"])
source = "7514cff44e"
patch16 = "3e8ad410e8f8b4d7fd3ded0d40c87d3bd4dbc51a"
patch17 = "ec9b41134c8b16305cbb9eb4473e67ae5dd55c45"
label = "//pkg/state:state_test"
name = "TestQADecoderReview"
frame = {"source": source, "component": patch16, "scheduler": patch17, "expected_target": label, "verdict": "INCOMPLETE"}
dest = result / "state-review-verdict.json"
image = "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
def now():
    return datetime.now(timezone.utc).isoformat()

def main():
    head = git("rev-parse", "HEAD")
    assert head.startswith(source) and git("rev-parse", "HEAD^") == patch17
    assert git("diff", "--name-only", patch17, head) == "pkg/state/graph_decode_callbacks_test.go"
    component_files = ["graph_decode_values_unsafe.go", "graph_decode_unsafe.go", "graph_encode_objects_unsafe.go", "graph_encode_stream_unsafe.go", "graph_encode_values_unsafe.go"]
    frame.update(source=head, components={})
    for filename in component_files:
        key = "pkg/state/" + filename
        blob = git("rev-parse", "HEAD:" + key)
        assert blob == git("rev-parse", patch16 + ":" + key)
        frame["components"][key] = blob
    invocation = str(uuid.uuid4())
    frame["invocation"] = invocation
    trace = result / "private.exec.json"
    events = result / "private.bep.json"
    rc = subprocess.run(["bazel", "--bazelrc=" + os.environ["QA_RC"], "test", "--config=x86_64", "--compilation_mode=opt", "--jobs=28", "--keep_going", "--nocache_test_results", "--flaky_test_attempts=1", "--strategy=TestRunner=remote", "--remote_default_exec_properties=container-image=" + image, "--test_output=errors", "--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_filter=^" + name + "$", "--remote_download_regex=.*/test[.](xml|log)$", "--build_event_json_file=" + str(events), "--execution_log_json_file=" + str(trace), "--invocation_id=" + invocation, label]).returncode
    frame.update(native_exit=rc, execution_finished=now())
    with events.open() as f:
        ev = [json.loads(line) for line in f if '"testResult"' in line]
    fs = [x for x in ev if x.get("id", {}).get("testResult", {}).get("label") == label and "testResult" in x]
    assert len(fs) == 1, len(fs)
    with trace.open() as f:
        actions = [a for _, a in execution_records(f) if a and a.get("mnemonic") == "TestRunner" and a.get("targetLabel") == label]
    assert len(actions) == 1
    a = actions[0]
    assert a["runner"] == "remote" and not a.get("cacheHit"), a["runner"]
    testdir = Path("bazel-testlogs/pkg/state/state_test")
    raw = (testdir / "test.xml").read_bytes()
    log = (testdir / "test.log").read_bytes()
    bound = []
    for filename, body in [("test.xml", raw), ("test.log", log)]:
        outputs = [p for p in a["actualOutputs"] if p["path"].endswith("/" + filename)]
        assert len(outputs) == 1 and outputs[0]["digest"]["hash"] == sha256(body).hexdigest()
        bound.append(outputs[0]["digest"])
    nodes = ET.fromstring(raw).findall(".//testcase")
    outcomes = {n.get("name"): "skip" if n.find("skipped") is not None else "fail" if n.find("failure") is not None or n.find("error") is not None else "pass" for n in nodes}
    expected = {name, *[name+"/"+case for case in ("map_views","three_bytes","four_bytes","interface_slot","map_value","map_pointer","waiting_parent")], *[name+"/"+w+"/"+o for w in ("three_bytes","four_bytes") for o in ("left_first","right_first")]}
    assert set(outcomes) == expected and len(nodes) == len(expected) and "skip" not in outcomes.values(), outcomes
    assert rc in (0,3) and (rc == 0) == all(v == "pass" for v in outcomes.values())
    assert fs[0]["testResult"]["status"] == ("PASSED" if rc == 0 else "FAILED")
    frame.update(outcomes=outcomes, bound_reports=bound, observed_target=label,
                 diagnostic=[l for l in log.decode().splitlines() if "word hooks=" in l or "parent hooks=" in l],
                 verified_at=now(), verdict="OBSERVED")
try:
    main()
finally:
    dest.write_text(json.dumps(frame, indent=2) + "\n")
    print(json.dumps(frame), flush=True)
