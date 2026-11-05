"""Run the two registered tests using the pinned native POSIX Bazel supplier."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import uuid

import qualification_report as report
import qualify as q


BASE = q.PRODUCT
PRODUCT = "06a7dd4635051800d646d33f8bfaf3a3b7055839"
SUPPLIER = "c2f5f79dce24877679cede14b85666b7ca65c6de"
PACKAGE = "pkg/tcpip/faketime"
GO = f"//{PACKAGE}:faketime_test"
NOGO = f"//{PACKAGE}:faketime_nogo"
CASES = {GO: {"TestManualClockAdvance", "TestManualClockAfterFunc"} |
         {"TestManualClockAfterFunc/" + s for s in ("before_timeout1", "timeout1", "timeout2", "after_timeout2")},
         NOGO: {PACKAGE + "/faketime_nogo"}}
BAZEL = q.TMP / "bin" / "bazel"
# c7606e0bf9 native_packages/run.py native_actions, with the two pinned
# release worker placements and both ordinary test executables.
PROJECTION = """
select(has("spawn")) | .spawn |
{m:.mnemonic, r:.runner, c:(.cacheHit//false), s:((.status//"")!=""), e:(.exitCode//0),
 p:[.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]} +
(if .mnemonic=="TestRunner" or ((.exitCode//0)!=0 or (.status//"")!="") then
 {l:.targetLabel, x:.args[0], d:.digest} else {} end) +
(if .mnemonic=="TestRunner" then {t:.args[1:]} else {} end)
"""


def actions(path, arch):
    counts = Counter()
    tests, bad = [], []
    platform = {"Arch": arch, "OSFamily": "linux", "container-image": "docker://gcr.io/distroless/static-debian13@" + q.PIN[arch],
                "recycle-runner": "false", "workload-isolation-type": "oci"}
    with subprocess.Popen([str(q.TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        with subprocess.Popen(["jq", "-c", PROJECTION], stdin=decoder.stdout, stdout=subprocess.PIPE, text=True) as rows:
            decoder.stdout.close()
            for line in rows.stdout:
                row = json.loads(line)
                own = {pair["name"]: pair["value"] for pair in row["p"]} == platform
                runner = row["r"] or "unknown"
                counts[runner] += 1
                if (runner not in ("remote", "remote cache hit") or not row["c"] and runner == "remote" and not own):
                    counts["wrong_execution"] += 1
                if row["e"] or row["s"]:
                    counts["failed"] += 1
                    if len(bad) < 12:
                        bad.append({k: row.get(k) for k in ("m", "l", "x", "d", "e", "s")})
                if row["m"] == "TestRunner":
                    row["l"] = re.sub(r"^@@?//", "//", row["l"])
                    tests.append({k: row[k] for k in ("l", "x", "t", "d", "e", "s", "c", "r")} |
                                 {"matched_platform": own})
            assert rows.wait() == 0
        assert decoder.wait() == 0
    return {"counts": dict(counts), "tests": tests, "failures": bad, "expected_platform": platform}


def events(path):
    selected = {"results": [], "summaries": {}, "completed": [], "failures": [], "aborted": []}
    if not path.exists():
        return selected
    for line in path.open():
        try:
            event = json.loads(line)
        except ValueError:
            selected["truncated"] = True
            continue
        ident = event.get("id", {})
        if "started" in event:
            selected.update(invocation=event["started"]["uuid"], bazel=event["started"].get("buildToolVersion"))
        if "finished" in event:
            selected.update(exit=event["finished"]["exitCode"].get("code", 0), finished=event["finished"].get("finishTime"))
        if (key := ident.get("targetCompleted")) and key.get("label") in CASES and not key.get("aspect"):
            selected["completed"].append([key["label"], key["configuration"]["id"], event.get("completed", {}).get("success", False)])
        if "testResult" in event:
            result = event["testResult"]
            selected["results"].append({"id": ident["testResult"], "status": result.get("status"),
                                        "cached_local": bool(result.get("cachedLocally")),
                                        "cached_remote": bool(result.get("executionInfo", {}).get("cachedRemotely"))})
        if "testSummary" in event:
            selected["summaries"][ident["testSummary"]["label"]] = event["testSummary"].get("overallStatus")
        if "action" in event and not event["action"].get("success") and len(selected["failures"]) < 12:
            action = event["action"]
            selected["failures"].append({"owner": ident.get("actionCompleted", {}).get("label"), "type": action.get("type"),
                                         "codes": [v["code"] for v in action.get("failureDetail", {}).values()
                                                   if isinstance(v, dict) and "code" in v]})
        if "aborted" in event and len(selected["aborted"]) < 12:
            selected["aborted"].append({"owner": next((v.get("label") for v in ident.values() if isinstance(v, dict)), None),
                                       "reason": event["aborted"].get("reason")})
    return selected


def phase(arch):
    work = q.TMP / arch
    work.mkdir()
    invocation = str(uuid.uuid4())
    row = {"arch": arch, "source": PRODUCT, "base": BASE, "supplier": SUPPLIER, "status": "INCOMPLETE",
           "expected_cases": {label: sorted(names) for label, names in CASES.items()}, "requested": invocation, "started": q.now()}
    q.save(arch, row)
    exe = "//test/rbe/release_prewarmer:" + arch
    try:
        assert q.git("rev-parse", "HEAD") == PRODUCT and not q.git("diff", "--name-only", "HEAD")
        flags = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
                 "--extra_execution_platforms=" + exe, "--host_platform=" + exe,
                 "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
                 "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
                 "--extra_toolchains=//test/rbe/protoc_check:static_python" + ("_arm64" if arch == "arm64" else ""),
                 "--noincompatible_exclude_starlark_flags_from_exec_config", "--features=fully_static_link",
                 "--incompatible_use_native_posix_test_wrapper", "--nocache_test_results", "--test_env=GO_TEST_WRAP_TESTV=1",
                 "--build_tag_filters=-network_plugins", "--keep_going", "--lockfile_mode=off", "--jobs=32", "--color=no",
                 "--curses=no", "--remote_download_outputs=toplevel", "--remote_accept_cached",
                 "--aspects=//tools/nogo:defs.bzl%nogo_aspect_tricorder", "--output_groups=+tricorder",
                 "--nobuild_event_json_file_path_conversion", "--build_event_publish_all_actions",
                 "--invocation_id=" + invocation, "--build_event_json_file=" + str(work / "events"),
                 "--execution_log_compact_file=" + str(work / "spawns")]
        with (work / "log").open("wb") as out:
            child = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "2100", str(BAZEL), *q.PROOF,
                                    "test", *flags, GO, NOGO], stdout=out, stderr=subprocess.STDOUT)
        row.update(native_exit=child.returncode, returned=q.now(), flags=flags[:-3],
                   status="INCOMPLETE" if child.returncode in (0,124,137) else "FAIL")
        q.save(arch, row)
        row["events"] = data = events(work / "events")
        row["diagnostics"] = q.diagnostics(work / "log")
        text = (work / "log").read_text(errors="replace")
        row["unrecognized_options"] = sorted(set(re.findall(r"(?i)(?:unrecognized option|unknown option):?\s*['\x22]?(--[a-zA-Z0-9_:-]+)", text)))[:6]
        q.save(arch, row)
        if (work / "spawns").is_file():
            row["actions"] = actions(work / "spawns", arch)
            q.save(arch, row)
        if not data["results"]:
            return False
        execroot = Path(subprocess.check_output([str(BAZEL), *q.PROOF, "info", "execution_root"], text=True,
                                                stderr=subprocess.DEVNULL, timeout=90).strip())
        cap = report.capture(work / "events", work / "sanitized", execroot, work / "reports", expected_kind="test")
        row["capture_error_count"] = len(cap["errors"])
        row["xml"], row["log_evidence"] = {}, {}
        for out in cap["reports"]:
            label = out["identity"]["label"]
            if label not in CASES:
                continue
            local = work / out["path"]
            if out["name"] == "test.log":
                log = local.read_text(errors="replace")
                row["log_evidence"][label] = {"sha256": out["sha256"], "bytes": out["bytes"], "pass": "PASS" in log.splitlines()}
            if out["name"] == "test.xml":
                row["xml"][label] = {"sha256": out["sha256"], "bytes": out["bytes"],
                                      **report.xml_cases(local, frozenset(CASES[label]))}
        q.save(arch, row)
        row["binaries"], row["filtered"] = {}, {}
        pkg = Path("bazel-bin") / PACKAGE
        for label, path in ((GO, pkg / "faketime_test_" / "faketime_test"), (NOGO, pkg / "faketime_nogo")):
            row["binaries"][label] = q.elf(path)
            q.save(arch, row)
        for target_arch in ("amd64", "arm64"):
            raw = (pkg / ("faketime_nogo." + target_arch + ".findings")).read_bytes()
            rows = json.loads(raw)
            row["filtered"][target_arch] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                                            "empty": rows in (None, [])}
        q.save(arch, row)
        assert child.returncode == data["exit"] == 0 and data["invocation"] == invocation and not data.get("truncated")
        assert len(data["results"]) == 2 and len(data["completed"]) == 2 and all(c[2] for c in data["completed"])
        owners = {(c[0],c[1]) for c in data["completed"]}
        assert {c[0] for c in owners} == set(CASES) and data["summaries"] == dict.fromkeys(CASES, "PASSED")
        assert {(r["id"]["label"],r["id"]["configuration"]["id"]) for r in data["results"]} == owners
        assert all(r["status"] == "PASSED" and not r["cached_local"] and not r["cached_remote"] for r in data["results"])
        actual = row["actions"]
        assert not actual["counts"].get("failed") and not actual["counts"].get("wrong_execution")
        assert Counter(t["l"] for t in actual["tests"]) == Counter(CASES.keys())
        for test in actual["tests"]:
            assert test["r"] == "remote" and not test["c"] and test["matched_platform"] and not test["e"] and not test["s"]
            assert Path(test["x"]).name == {"amd64": "posix_tw", "arm64": "compiled_posix_test_wrapper"}[arch]
            name = test["l"].split(":")[1]
            assert test["t"][0].startswith(PACKAGE + "/") and Path(test["t"][0]).name == name
            if test["l"] == NOGO:
                assert test["t"][1:4] == ["filter", "-test", "-text"]
                assert sorted(test["t"][4:]) == ["nogo.amd64.findings", "nogo.arm64.findings"]
        assert not cap["errors"]
        for label, expected in CASES.items():
            xml = row["xml"][label]
            assert (xml["passed"], xml["failed"], xml["skipped"]) == (len(expected), 0, 0)
            assert set(xml["required_passed"]) == expected
            binary = row["binaries"][label]
            assert binary["machine"] == {"amd64": 62, "arm64": 183}[arch] and binary["load"] and binary["entry"]
        assert all(item["empty"] for item in row["filtered"].values()) and row["log_evidence"][NOGO]["pass"]
        row["status"] = "PASS"
    except (OSError, AssertionError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        row["collection_error"] = {"type":type(exc).__name__,"line":traceback.extract_tb(exc.__traceback__)[-1].lineno}
    finally:
        row["verified"] = q.now()
        q.save(arch, row)
    return row["status"] == "PASS"


if __name__ == "__main__":
    arg, = sys.argv[1:]
    if arg == "verdict":
        results = {arch: json.loads((q.OUT / (arch + ".json")).read_text())["status"]
                   for arch in ("amd64", "arm64") if (q.OUT / (arch + ".json")).exists()}
        good = results == {"amd64": "PASS", "arm64": "PASS"}
        q.save("verdict", {"source": PRODUCT, "supplier": SUPPLIER, "carrier": os.environ["GITHUB_SHA"],
                           "statuses": results, "pass": good, "verified_at": q.now(),
                           "prior_native_nogo": 13080411787, "prior_arm_native_xml": 12986015991})
    else:
        assert arg in ("amd64","arm64")
        good = phase(arg)
    raise SystemExit(0 if good else 1)
