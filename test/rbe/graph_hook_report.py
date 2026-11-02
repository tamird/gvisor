"""Qualify the graph hook boundary with existing suites and restore benchmarks."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback
from xml.etree import ElementTree

from qualification_report import capture, file_record, retained_reports, xml_cases

ROOT = Path(os.environ["RUNNER_TEMP"]) / "hook-boundary"
OUT = ROOT / "proof"
PRODUCT = os.environ["PRODUCT"]
BASE = os.environ["BASE"]
CORE = "//pkg/state:state_test"
GENERATED = "//pkg/state/tests:tests_test"
LABELS = {CORE: "core", GENERATED: "generated"}
BZ = ["bazel", "--bazelrc=" + os.environ["RUNNER_TEMP"] + "/tcp.bazelrc"]
FLAGS = ["--config=x86_64", "--jobs=48", "--remote_download_outputs=toplevel",
         "--nocache_test_results", "--test_timeout=60", "--test_env=GO_TEST_WRAP_TESTV=1",
         "--build_event_json_file_path_conversion=false", "--test_output=errors"]
GIT = ["git", "-c", "safe.directory=" + str(Path.cwd())]
LEAVES = {"struct_keys", "struct_values", "array_keys", "array_values", "interface_keys",
          "interface_values", "map_passed_to_LoadValue"}
REQUIRED = {"tests/TestMapLoadHookReceivers/" + mode + "/" + leaf
            for mode in ("wire", "graph") for leaf in LEAVES}


def write(name: str, data: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / (name + ".json")).write_text(json.dumps(data, indent=2) + "\n")


def select(source: str) -> None:
    subprocess.run(GIT + ["checkout", "-q", "--detach", source], check=True)
    assert subprocess.check_output(GIT + ["rev-parse", "HEAD"], text=True).strip() == source
    subprocess.run(GIT + ["diff", "--quiet", "HEAD", "--"], check=True)


def census(path: Path) -> dict[str, str]:
    rows = {}
    for case in ElementTree.parse(path).iter("testcase"):
        key = case.attrib["classname"] + "/" + case.attrib["name"]
        assert key not in rows, f"Repeated XML case: {key}"
        rows[key] = ("FAIL" if case.find("failure") is not None or case.find("error") is not None else
                     "SKIP" if case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed" else "PASS")
    return rows


def invoke(name: str, source: str, owners: list[str], *, race: bool = False,
           shards: int = 1, bench: bool = False) -> dict:
    folder = ROOT / name
    folder.mkdir(parents=True, exist_ok=True)
    row = {"status": "INCOMPLETE", "source": source, "owners": owners, "attempts": [],
           "coordinator": {"machine": os.uname().machine, "kernel": os.uname().release, "cpus": os.cpu_count()}}
    write(name, row)
    try:
        select(source)
        assert os.uname().machine == "x86_64"
        output_base = Path(subprocess.check_output(BZ + ["info", "output_base"], stderr=subprocess.DEVNULL, text=True).strip())
        args = BZ + ["test", *FLAGS, "--strategy=TestRunner=" + ("local" if bench else "remote"),
                     "--test_sharding_strategy=" + (f"forced={shards}" if shards > 1 else "disabled"),
                     "--build_event_json_file=" + str(folder / "bep")]
        if race:
            args.append("--config=race")
        if bench:
            args += ["--noremote_upload_local_results", "--test_arg=-test.run=^$",
                     "--test_arg=-test.bench=^BenchmarkGraphLoad$|^BenchmarkDecoding$/^ptr$/^state$",
                     "--test_arg=-test.benchtime=32x", "--test_arg=-test.benchmem", "--test_arg=-test.count=1"]
        row["started_at_ns"] = time.time_ns()
        with (folder / "command.log").open("wb") as log:
            code = subprocess.run(["timeout", "-k", "10s", "14m", *args, *owners], stdout=log, stderr=log).returncode
        row.update(native_exit=code, finished_at_ns=time.time_ns(), status="FAIL" if code else "INCOMPLETE")
        write(name, row)
        events = [json.loads(line) for line in (folder / "bep").read_text().splitlines() if line.strip()]
        started, = [e["started"] for e in events if "started" in e]
        finished, = [e["finished"] for e in events if "finished" in e]
        row["invocation"] = started["uuid"]
        row["bazel_exit"] = int(finished["exitCode"].get("code", 0))
        row["bazel_finished"] = finished.get("finishTime", finished.get("finishTimeMillis"))
        assert row["bazel_exit"] == code
        capture(folder / "bep", folder / "events.jsonl", output_base, folder / "reports")
        _, reports, errors = retained_reports(Path(str(folder / "events.jsonl") + ".manifest.json"))
        assert not errors, "Original report capture incomplete"
        tests = [e for e in events if "testResult" in e.get("id", {})]
        completed = [e for e in events if "targetCompleted" in e.get("id", {}) and not e["id"]["targetCompleted"].get("aspect")]
        assert len(completed) == len(owners) and {e["id"]["targetCompleted"]["label"] for e in completed} == set(owners)
        assert all(e["completed"].get("success") for e in completed)
        assert len(tests) == len(owners) * shards
        summaries = [e for e in events if "testSummary" in e.get("id", {})]
        assert len(summaries) == len(owners) and {e["id"]["testSummary"]["label"] for e in summaries} == set(owners)
        row["summaries"] = {e["id"]["testSummary"]["label"]: e["testSummary"]["overallStatus"] for e in summaries}
        observed = {}
        for owner in owners:
            selected = [e for e in tests if e["id"]["testResult"]["label"] == owner]
            assert len(selected) == shards
            assert {int(e["id"]["testResult"].get("shard", 1)) for e in selected} == set(range(1, shards + 1))
            configs = {e["id"]["testResult"]["configuration"]["id"] for e in selected}
            assert len(configs) == 1 and all(configs)
            expected_config, = configs
            assert next(e["id"]["targetCompleted"]["configuration"]["id"] for e in completed if e["id"]["targetCompleted"]["label"] == owner) == expected_config
            for event in selected:
                identity = event["id"]["testResult"]
                result = event["testResult"]
                attempt = {"owner": owner, "configuration": expected_config, "shard": int(identity.get("shard", 1)),
                           "status": result["status"], "strategy": result.get("executionInfo", {}).get("strategy"),
                           "cached": bool(result.get("cachedLocally") or result.get("executionInfo", {}).get("cachedRemotely"))}
                row["attempts"].append(attempt)
                if attempt["status"] != "PASSED":
                    row["status"] = "FAIL"
                write(name, row)
                assert int(identity.get("run", 1)) == int(identity.get("attempt", 1)) == 1
                assert not attempt["cached"] and attempt["strategy"] == ("local" if bench else "remote")
                for suffix in ("xml", "log"):
                    path, = [p for (a, n), p in reports.items() if a.owner.label == owner and a.owner.configuration == expected_config and a.shard == attempt["shard"] and a.run == a.attempt == 1 and n == "test." + suffix]
                    attempt[suffix] = file_record(path)
                    if bench and suffix == "log":
                        lines = re.findall(r"^(Benchmark\S+)\s+(\d+)\s+([\d.]+) ns/op\s+(\d+) B/op\s+(\d+) allocs/op$", path.read_text(), re.M)
                        assert len(lines) == 2 and {re.sub(r"-\d+$", "", x[0]) for x in lines} == {"BenchmarkDecoding/ptr/state", "BenchmarkGraphLoad"}, lines
                        attempt["benchmarks"] = [{"name": re.sub(r"-\d+$", "", n), "iterations": int(it), "ns/op": float(ns), "B/op": int(b), "allocs/op": int(a)} for n, it, ns, b, a in lines]
                        assert all(x["iterations"] == 32 and x["ns/op"] > 0 for x in attempt["benchmarks"])
                    if not bench:
                        shutil.copyfile(path, OUT / f"{name}-{LABELS[owner]}-{attempt['shard']}.{suffix}")
                        if suffix == "xml":
                            attempt["counts"] = xml_cases(path)
                            cases = census(path)
                            assert cases and not observed.keys() & cases.keys()
                            observed.update(cases)
                            if "FAIL" in cases.values():
                                row["status"] = "FAIL"
                assert attempt["status"] == "PASSED"
        if not bench:
            required = ({"state/TestGraphCallbackOrder"} if CORE in owners else set()) | (REQUIRED if GENERATED in owners else set())
            assert all(observed.get(k) == "PASS" for k in required), "Missing required passing hook cases"
            row["case_counts"] = dict(Counter(observed.values()))
            row["case_census_sha256"] = hashlib.sha256(json.dumps(sorted(observed.items())).encode()).hexdigest()
            (ROOT / (name + "-census.json")).write_text(json.dumps(observed))
            assert "FAIL" not in observed.values()
        assert code == 0 and set(row["summaries"].values()) == {"PASSED"}
        subprocess.run(GIT + ["diff", "--quiet", "HEAD", "--"], check=True)
        row["status"] = "PASS"
    except Exception as error:
        row["collection_error"] = {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
    row["verified_at_ns"] = time.time_ns()
    if end := row.get("finished_at_ns"):
        row["execution_to_verdict_seconds"] = (row["verified_at_ns"] - end) / 1e9
    write(name, row)
    print(name, row["status"], row.get("native_exit"), flush=True)
    return row


def race() -> bool:
    rows = [invoke("race-core", PRODUCT, [CORE], race=True),
            invoke("race-generated", PRODUCT, [GENERATED], race=True, shards=4)]
    result = {"status": "FAIL" if any(r["status"] == "FAIL" for r in rows) else "INCOMPLETE",
              "phases": [r["status"] for r in rows]}
    try:
        normal = json.loads((ROOT / "normal-census.json").read_text())
        actual = json.loads((ROOT / "race-core-census.json").read_text()) | json.loads((ROOT / "race-generated-census.json").read_text())
        primitive = "int int8 int16 int32 int64 uint uintptr uint8 uint16 uint32 uint64 float32 float64 complex64 complex128 string".split()
        invalid = re.findall(r'^\s*"([a-z-]+)":', Path("pkg/state/tests/register_test.go").read_text(), re.M)
        assert len(primitive) == 16 and len(invalid) == 11
        extras = {"tests/" + k: "PASS" for k in ["TestRegisterPrimitives", "TestRegisterBad", "TestRegisterTypeOnlyStruct",
                  *(f"TestRegisterPrimitives/{kind}/{t}" for kind in ("struct", "other") for t in primitive),
                  *("TestRegisterBad/" + k for k in invalid)]}
        expected = normal | extras
        # Race builds deliberately validate types during registration.
        intentional_skips = {"state/TestLazyValidation", "state/TestRegisterIsLazy"}
        for path in ("pkg/state/types.go", "pkg/state/types_test.go", "pkg/state/state_race.go"):
            assert subprocess.check_output(GIT + ["rev-parse", BASE + ":" + path]) == subprocess.check_output(GIT + ["rev-parse", PRODUCT + ":" + path])
        assert all(normal.get(k) == "PASS" for k in intentional_skips)
        expected.update(dict.fromkeys(intentional_skips, "SKIP"))
        result["intentional_race_skips"] = sorted(intentional_skips)
        result.update(expected_count=len(expected), actual_count=len(actual),
                      missing=sorted(expected.keys() - actual.keys()), extra=sorted(actual.keys() - expected.keys()),
                      changed=[k for k in expected.keys() & actual.keys() if expected[k] != actual[k]],
                      race_only_registration_cases=len(extras))
        assert actual == expected, "Race shard census differs from normal plus the race-only fixture"
        result["status"] = "PASS" if all(r["status"] == "PASS" for r in rows) else "FAIL"
    except Exception as error:
        result["collection_error"] = {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
    write("race-census", result)
    return result["status"] == "PASS"


def benchmark() -> bool:
    for path in ("pkg/state/tests/bench.go", "pkg/state/tests/bench_test.go", "pkg/state/tests/graph.go"):
        assert subprocess.check_output(GIT + ["rev-parse", BASE + ":" + path]) == subprocess.check_output(GIT + ["rev-parse", PRODUCT + ":" + path])
    rows = []
    for pair in range(6):
        for arm in (("stock", "fixed") if pair % 2 == 0 else ("fixed", "stock")):
            row = invoke(f"bench-{pair}-{arm}", BASE if arm == "stock" else PRODUCT, [GENERATED], bench=True)
            rows.append({"pair": pair, "arm": arm, **row})
            if row["status"] != "PASS":
                write("benchmark", {"status": row["status"], "rows": rows})
                return False
    write("benchmark", {"status": "PASS", "baseline": BASE, "candidate": PRODUCT,
                        "scope": "Two existing warm restore benchmarks; same Linux coordinator; six alternating pairs of 32 iterations", "rows": rows})
    select(PRODUCT)
    return True


ROOT.mkdir(parents=True, exist_ok=True)
OUT.mkdir(exist_ok=True)
phase = sys.argv[1]
if phase == "normal":
    passed = invoke("normal", PRODUCT, [CORE, GENERATED])["status"] == "PASS"
elif phase == "race":
    passed = race()
elif phase == "benchmark":
    passed = benchmark()
else:
    phases = {name: json.loads((OUT / (name + ".json")).read_text())["status"] if (OUT / (name + ".json")).exists() else "MISSING"
              for name in ("normal", "race-core", "race-generated", "race-census", "benchmark")}
    passed = set(phases.values()) == {"PASS"}
    write("verdict", {"status": "PASS" if passed else ("FAIL" if "FAIL" in phases.values() else "INCOMPLETE"), "source": PRODUCT, "base": BASE,
                      "phases": phases, "stock_observation": "https://github.com/tamird/gvisor/actions/runs/41390699623/artifacts/13116824768",
                      "verified_at_ns": time.time_ns()})
raise SystemExit(not passed)
