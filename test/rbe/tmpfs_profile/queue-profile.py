"""Compare two cold kernel decodes of one frozen 50,000-file checkpoint."""
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import time
from xml.etree import ElementTree

phase = sys.argv[1]
sys.argv[1] = "helpers"
shared = runpy.run_path(str(Path(__file__).with_name("pair-run.py")))
root, out = shared["root"], shared["out"]
bz, opts, gnu = shared["bz"], shared["opts"], shared["gnu"]
command, record, digests = shared["command"], shared["record"], shared["file_digests"]
capture = root / "live-00-fixed"
bench = "//test/benchmarks/checkpointfile:checkpointfile_test"
owner = "//pkg/state:state_test"


def read_xml(name, label):
    path = "bazel-testlogs/" + label[2:].replace(":", "/") + "/test.xml"
    raw = subprocess.check_output(gnu + ["cat", path])
    (out / (name + ".xml")).write_bytes(raw)
    cases = list(ElementTree.fromstring(raw).iter("testcase"))
    assert cases
    active, skipped, failed = [], [], []
    for case in cases:
        case_name = case.attrib["name"]
        if any(case.find(tag) is not None for tag in ("failure", "error")):
            failed.append(case_name)
        elif case.find("skipped") is not None or case.get("status") in ("notrun", "suppressed") or case.get("result") == "suppressed":
            skipped.append(case_name)
        else:
            active.append(case_name)
    return {"active": active, "skipped": skipped, "failed": failed, "sha256": hashlib.sha256(raw).hexdigest()}


if phase in ("normal", "race"):
    shared["select"]("fixed")
    args = ["test", *opts, "--nocache_test_results", "--test_env=GO_TEST_WRAP_TESTV=1",
            "--test_arg=-test.v", "--test_sharding_strategy=disabled", "--test_timeout=180", "--strategy=TestRunner=local"]
    if phase == "race":
        args.append("--config=race")
    rc = command(phase, bz + args + [owner], allow_failure=True)
    xml = read_xml(phase, owner)
    record(phase + ".json", {"exit": rc, **xml})
    wanted = {"TestCompletionQueueCapacity/" + c for c in ("empty", "undersized", "sufficient")}
    wanted |= {"TestGraphCallbackOrder", "TestGraphDependencyCycle/direct_two_node_cycle",
               "TestGraphDecodeMapPopulatedBeforeCallbacks", "TestPrintGraphDeterministic"}
    assert rc == 0 and not xml["failed"] and wanted <= set(xml["active"])
elif phase == "capture":
    shared["live"](0, "fixed")
    xml = read_xml("capture", "//runsc/container:container_test")
    assert xml["active"] == ["TestCheckpointTmpfsTreeForProfile"] and not xml["failed"] and not xml["skipped"]
    record("frozen.json", digests(capture))
elif phase in ("stock", "candidate"):
    sha = os.environ["BASE" if phase == "stock" else "PRODUCT"]
    subprocess.run(["git", "checkout", "-q", "--detach", sha], check=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == sha
    subprocess.run(["git", "diff", "--quiet"], check=True)
    frozen = json.loads((out / "frozen.json").read_text())
    assert digests(capture) == frozen
    command(phase + "-build", bz + ["build", *opts, bench])
    exe = str(shared["output_file"](phase + "-path", bench))
    exe_hash = subprocess.check_output(gnu + ["sha256sum", exe], text=True).split()[0]
    profile = str(root / (phase + ".heap"))
    fetch = gnu + ["env", "GO_TEST_WRAP=0", "GOMAXPROCS=1", exe, "-checkpoint-dir=" + str(capture), "-test.cpu=1",
                   "-test.run=^$", "-test.bench=^BenchmarkCheckpointFile$/^DecodeGC$", "-test.benchtime=1x",
                   "-test.benchmem", "-test.memprofilerate=1", "-test.memprofile=" + profile]
    raw = command(phase + "-decode", fetch, get_stdout=True)
    row, = re.findall(r"^BenchmarkCheckpointFile/DecodeGC(?:-\d+)?\s+1\s+[^\n]+", raw, re.M)
    assert re.search(r"^PASS$", raw, re.M) and "allocs/op" in row and "B/op" in row and digests(capture) == frozen
    receipt = {"source": sha, "benchmark": row, "binary_sha256": exe_hash, "frozen": frozen,
               "benchmark_blob": subprocess.check_output(["git", "rev-parse", "HEAD:test/benchmarks/checkpointfile/checkpointfile.go"], text=True).strip()}
    record(phase + ".json", receipt)
    # Copy only the private profile to the host for the same Go symbolizer as the original.
    copy = root / (phase + "-host.pprof")
    with copy.open("wb") as writer:
        subprocess.run(gnu + ["cat", profile], stdout=writer, check=True)
    assert copy.stat().st_size > 0
    receipt["profile_sha256"] = hashlib.sha256(copy.read_bytes()).hexdigest()
    source = Path("pkg/state/graph_decode_callbacks_unsafe.go").read_text().splitlines()
    lines = {str(i): s.strip() for i, s in enumerate(source, 1) if "leaves = append(leaves," in s}
    assert len(lines) == 2
    receipt["queue_sample_lines"] = lines
    for metric, unit in (("alloc_space", "bytes"), ("alloc_objects", "count")):
        flags = ["go", "tool", "pprof", "-top", "-lines", "-nodecount=100000", "-nodefraction=0", "-unit=" + unit,
                 "-sample_index=" + metric, "-focus=[.]loadKernel$", str(copy)]
        result = subprocess.run(flags, text=True, capture_output=True, check=True)
        text = result.stdout
        # Preserve the original symbolized rows before interpreting the source positions.
        raw_rows = [row.strip() for row in text.splitlines() if ".runCallbacks" in row]
        receipt[metric] = {"exit": result.returncode, "callback_rows": raw_rows}
        record(phase + ".json", receipt)
        assert "Type: " + metric in text and "loadKernel" in text and ".appendCallback" in text
        # Retain only exact graph decoder source lines and their original numeric columns.
        selected = [s.strip() for s in text.splitlines() if any(
            re.search(r"graph_decode_callbacks_unsafe\.go:" + line + r"(?:\s|$)", s) for line in lines)
            and ".runCallbacks" in s]
        receipt[metric].update(queue_lines=selected, flat=sum(int(s.split()[0].removesuffix("B")) for s in selected))
        record(phase + ".json", receipt)
    receipt["finished_ns"] = time.time_ns()
    record(phase + ".json", receipt)
else:
    raise ValueError(phase)
