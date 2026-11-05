"""Run only the original failed KVM vDSO package, with the published tool declarations."""

from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
from urllib.parse import unquote, urlsplit
import uuid
import xml.etree.ElementTree as ET
import zipfile

import qualification_report as report
import qualify as q


PRODUCT = "f21351df388475233c79704843aa8b65137ec16b"
SUPPLIER = "c2f5f79dce24877679cede14b85666b7ca65c6de"
LEDGER = "2d421f5aad3aead4536178d83dcbaf23416d05bac76a7102d7d06037cf384dee"
OWNER = "//pkg/sentry/platform/kvm:vdso_test"
ORIGINAL = 13410799023
BAZEL = q.TMP / "bin" / "bazel"
PROJECTION = """
select(has("spawn")) | .spawn |
{m:.mnemonic, r:.runner, c:(.cacheHit//false), s:((.status//"")!=""), e:(.exitCode//0),
 p:[.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]} +
(if .mnemonic=="TestRunner" then {l:.targetLabel,x:.args[0],t:.args[1],d:.digest,tools:[.args[2:][] | select(startswith("--objcopy=") or startswith("--objdump="))]}
 elif ((.exitCode//0)!=0 or (.status//"")!="") then {l:.targetLabel} else {} end)
"""


def expected(arch: str) -> set[str]:
    raw = (q.TMP / "sentry-ledger.zip").read_bytes()
    assert len(raw) == 34237 and hashlib.sha256(raw).hexdigest() == LEDGER
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        prior = json.loads(archive.read(arch + ".json"))
    assert prior["source"] == "f35d725669ae45e7617cf66e7ceee2f0f6307490" and prior["native_exit"] == 1
    target = prior["targets"][OWNER]
    assert target["outcome"] == "FAILED" and target["fresh_native"]
    result, = target["results"]
    xml = result["xml"]
    assert xml["sha256"] == {"amd64": "1f2e86e466f4af56fb7aefbe0cddfe0060d7124f8e967c60826c6ae730587665",
                             "arm64": "72087f6e4756e9dcb6714ad2aa7d32954a93d25b24a46411b585c24de1d46a38"}[arch]
    assert xml["counts"] == {"passed": 8, "failed": {"amd64": 15, "arm64": 12}[arch], "skipped": 0}
    names = set(xml["named"]["passed"] + xml["named"]["failed"])
    assert len(names) == sum(xml["counts"].values())
    return names


def identity(label: str) -> str:
    return label.removeprefix("@@").removeprefix("@")


def actions(path: Path, arch: str, targets: dict) -> dict:
    platform = {"Arch": arch, "OSFamily": "linux", "container-image": "docker://gcr.io/distroless/static-debian13@" + q.PIN[arch],
                "recycle-runner": "false", "workload-isolation-type": "oci"}
    counts, failures = Counter(), []
    with subprocess.Popen([str(q.TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        with subprocess.Popen(["jq", "-c", PROJECTION], stdin=decoder.stdout, stdout=subprocess.PIPE, text=True) as rows:
            decoder.stdout.close()
            for line in rows.stdout:
                a = json.loads(line)
                native = {p["name"]: p["value"] for p in a["p"]} == platform
                counts[a["r"]] += 1
                if a["r"] not in ("remote", "remote cache hit") or (not a["c"] and not native):
                    counts["wrong_execution"] += 1
                if a["e"] or a["s"]:
                    counts["failed_actions"] += 1
                    if len(failures) < 16:
                        failures.append({"owner": a.get("l"), "mnemonic": a["m"], "exit": a["e"]})
                if a["m"] == "TestRunner":
                    label = identity(a["l"])
                    if label not in targets:
                        counts["unexpected_test"] += 1
                        continue
                    package, name = label[2:].split(":")
                    wrapper = {"amd64": "posix_tw", "arm64": "compiled_posix_test_wrapper"}[arch]
                    # The native wrapper can launch a second TestRunner spawn solely
                    # to generate XML. It is not another product test execution.
                    if a["t"] == "--generate-xml":
                        targets[label]["xml_generators"].append({"digest": a["d"], "exit": a["e"], "matched_platform": native})
                        continue
                    tools = {name: [arg.partition("=")[2] for arg in a["tools"] if arg.startswith("--" + name + "=")]
                             for name in ("objcopy", "objdump")}
                    targets[label]["actions"].append({
                        "declared_tool_paths": {name: [v for v in paths if re.fullmatch(r"[a-zA-Z0-9_+./@=-]{1,350}", v)] for name, paths in tools.items()},
                        "both_tool_args": all(len(paths) == 1 and "/" in paths[0] for paths in tools.values()),
                        "digest": a["d"], "exit": a["e"], "status_nonempty": a["s"],
                        "runner": a["r"], "cache_hit": a["c"], "matched_platform": native,
                        "native_wrapper": Path(a["x"]).name == wrapper,
                        "owner_executable": a["t"].startswith(package + "/") and Path(a["t"]).name == name,
                    })
            assert rows.wait() == 0
        assert decoder.wait() == 0
    return {"runners": dict(counts), "failures": failures, "expected_platform": platform}


def case_name(value: str) -> str:
    # Only case identifiers, never diagnostics, skipped reasons or XML content.
    if len(value) <= 400 and value.isascii() and value.isprintable():
        return value
    return "case-sha256:" + hashlib.sha256(value.encode()).hexdigest()


def xml_cases(file: dict, label: str, row: dict) -> None:
    """Only read the XML advertised by this TestResult. Bind any fallback to its CAS digest."""
    uri = urlsplit(file.get("uri", ""))
    digest = file.get("digest")
    size = int(file["length"]) if "length" in file else None
    root = Path("bazel-out").resolve(strict=True)
    owner_path = label[2:].replace(":", "/")
    if uri.scheme == "bytestream" and (match := re.search(r"/blobs/([0-9a-f]{64})/([0-9]+)$", uri.path)):
        digest, size = match[1], int(match[2])
    if digest:
        assert re.fullmatch("[0-9a-f]{64}", digest)
        row["advertised_sha256"] = digest
    if size is not None:
        row["advertised_bytes"] = size
    if uri.scheme == "file" and uri.netloc in ("", "localhost"):
        path = Path(unquote(uri.path))
        via = "BEP_FILE"
    else:
        assert digest and size is not None and uri.scheme == "bytestream"
        path = Path("bazel-testlogs") / owner_path / "test.xml"
        via = "MATERIALIZED_CAS"
    path = path.resolve(strict=True)
    rel = path.relative_to(root)
    assert rel.parts[1:] == ("testlogs", *owner_path.split("/"), "test.xml")
    assert path.is_file() and 0 < path.stat().st_size <= 8 * 1024 * 1024
    raw = path.read_bytes()
    h = hashlib.sha256(raw).hexdigest()
    assert (not digest or h == digest) and (size is None or len(raw) == size)
    row.update(via=via, sha256=h, bytes=len(raw))
    counts = report.xml_cases(path)
    doc = ET.fromstring(raw)
    messages = " ".join(list(doc.itertext()) + [str(v) for x in doc.iter() for v in x.attrib.values()]).lower()
    row["diagnostics"] = [k for k in ("executable file not found", "no such file or directory", "objdump", "objcopy",
        "permission denied", "unexpected instruction", "exec format error") if k in messages]
    groups = {k: [] for k in ("passed", "failed", "skipped")}
    generic = 0
    for case in doc.iter("testcase"):
        name = case.get("name", "")
        if not re.match(r"^(Test|Example|Fuzz)", name):
            generic += 1
            continue
        kind = ("failed" if case.find("failure") is not None or case.find("error") is not None else
                "skipped" if case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed" else "passed")
        groups[kind].append(case_name(name))
    row.update(counts={k: counts[k] for k in groups}, named=groups, unnamed=generic)
    assert sum(map(len, groups.values())) + generic == sum(row["counts"].values())


def events(path: Path, targets: dict, configs: dict) -> dict:
    data = {"failures": [], "aborted": [], "malformed_lines": 0}
    if not path.is_file():
        return data
    with path.open() as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                data["malformed_lines"] += 1
                continue
            ident = event.get("id", {})
            if "started" in event:
                data.update(invocation=event["started"]["uuid"], bazel=event["started"].get("buildToolVersion"))
            if "finished" in event:
                data.update(exit=event["finished"]["exitCode"].get("code", 0), finished=event["finished"].get("finishTime"))
            if "configuration" in event:
                configs[ident["configuration"]["id"]] = event["configuration"].get("cpu")
            if (key := ident.get("targetCompleted")) and (label := identity(key["label"])) in targets and not key.get("aspect"):
                targets[label]["completed"].append({"config": key["configuration"]["id"], "built": bool(event.get("completed", {}).get("success"))})
            if (key := ident.get("testSummary")) and (label := identity(key["label"])) in targets:
                summary = event.get("testSummary", {})
                targets[label]["summaries"].append({"config": key["configuration"]["id"], "status": summary.get("overallStatus"),
                    "runs": summary.get("totalRunCount"), "shards": summary.get("shardCount")})
            if (key := ident.get("testResult")) and (result := event.get("testResult")):
                label = identity(key["label"])
                if label not in targets:
                    data["unexpected_test"] = True
                    continue
                entry = {"config": key["configuration"]["id"], "run": key.get("run"), "shard": key.get("shard"), "attempt": key.get("attempt"),
                         "status": result["status"], "cached_local": bool(result.get("cachedLocally")),
                         "cached_remote": bool(result.get("executionInfo", {}).get("cachedRemotely"))}
                targets[label]["results"].append(entry)
                xml = entry["xml"] = {}
                try:
                    file, = [f for f in result.get("testActionOutput", []) if f.get("name") == "test.xml"]
                    xml_cases(file, label, xml)
                except (OSError, ValueError, KeyError, AssertionError, ET.ParseError) as exc:
                    xml["error"] = fault(exc)
            if (action := event.get("action")) and not action.get("success") and len(data["failures"]) < 16:
                data["failures"].append({"owner": ident.get("actionCompleted", {}).get("label"), "type": action.get("type"),
                    "codes": [v["code"] for v in action.get("failureDetail", {}).values() if isinstance(v, dict) and "code" in v]})
            if "aborted" in event and len(data["aborted"]) < 16:
                data["aborted"].append({"owner": next((v.get("label") for v in ident.values() if isinstance(v, dict)), None),
                                        "reason": event["aborted"].get("reason")})
    return data


def fault(exc: Exception) -> dict:
    return {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}


def classify(t: dict, configs: dict, cpu: str) -> str:
    match t:
        case {"results": [r], "actions": [a], "completed": [b], "summaries": [s]}:
            pass
        case {"results": [], "actions": []}:
            return "NOT_EXECUTED"
        case _:
            return "INCOMPLETE"
    t["cpu"] = configs.get(r["config"])
    t["fresh_native"] = (t["cpu"] == cpu and r["config"] == b["config"] == s["config"] and
        (r["run"], r["shard"], r["attempt"]) == (1, 1, 1) and not (r["cached_local"] or r["cached_remote"] or a["cache_hit"])
        and a["runner"] == "remote" and a["matched_platform"] and a["native_wrapper"] and a["owner_executable"])
    if r["status"] in ("FAILED", "TIMEOUT"):
        return "FAILED" if t["fresh_native"] else "UNQUALIFIED_FAILURE"
    if r["status"] != "PASSED":
        return "INCOMPLETE"
    if not t["fresh_native"] or not b["built"] or r["status"] != s["status"] or a["exit"] or a["status_nonempty"]:
        return "INCOMPLETE"
    xml = r["xml"]
    if "named" not in xml or xml.get("error"):
        return "MISSING_XML"
    names = xml["named"]
    if names["failed"] or xml["counts"]["failed"]:
        return "FAILED"
    if not names["passed"]:
        return "ALL_SKIPPED" if names["skipped"] else "NO_NAMED_CASES"
    if xml["unnamed"]:
        return "PARTIAL_CASE_NAMES"
    return "PASSED_WITH_SKIPS" if names["skipped"] else "PASSED"


def phase(arch: str) -> bool:
    work = q.TMP / arch
    work.mkdir()
    names = expected(arch)
    labels = [OWNER]
    invocation = str(uuid.uuid4())
    targets = {label: {k: [] for k in ("actions", "xml_generators", "results", "summaries", "completed")} for label in labels}
    row = {"arch": arch, "source": PRODUCT, "supplier": SUPPLIER, "baseline_artifact": ORIGINAL,
           "prior_zip_sha256": LEDGER, "upstream_head": "212b0add216acfcad2c20b2aaee101d8461d085f", "expected_named": len(names), "requested_count": len(labels),
           "targets": targets, "status": "INCOMPLETE", "requested_invocation": invocation, "started": q.now()}
    q.save(arch, row)
    cpu = {"amd64": "k8", "arm64": "aarch64"}[arch]
    try:
        assert q.git("rev-parse", "HEAD") == PRODUCT and not q.git("diff", "--name-only", "HEAD")
        platform = "//test/rbe/release_prewarmer:" + arch
        flags = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
                 "--extra_execution_platforms=" + platform, "--host_platform=" + platform,
                 "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
                 "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
                 "--extra_toolchains=//test/rbe/protoc_check:static_python" + ("_arm64" if arch == "arm64" else ""),
                 "--noincompatible_exclude_starlark_flags_from_exec_config", "--features=fully_static_link",
                 "--incompatible_use_native_posix_test_wrapper", "--nocache_test_results", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_env=PATH=/nonexistent",
                 "--runs_per_test=1", "--flaky_test_attempts=1", "--build_tag_filters=-network_plugins",
                 "--keep_going", "--lockfile_mode=off", "--jobs=32", "--color=no", "--curses=no",
                 "--remote_download_outputs=toplevel", "--remote_accept_cached",
                 "--remote_download_regex=^bazel-out/[^/]+/testlogs/pkg/sentry/platform/kvm/vdso_test/test[.]xml$",
                 "--nobuild_event_json_file_path_conversion", "--build_event_publish_all_actions",
                 "--invocation_id=" + invocation, "--build_event_json_file=" + str(work / "events"),
                 "--execution_log_compact_file=" + str(work / "spawns")]
        with (work / "log").open("wb") as out:
            child = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "1080", str(BAZEL), *q.PROOF,
                                    "test", *flags, *labels], stdout=out, stderr=subprocess.STDOUT)
        row.update(native_exit=child.returncode, returned=q.now())
        q.save(arch, row)
        configs = {}
        row["events"] = events(work / "events", targets, configs)
        row["diagnostics"] = q.diagnostics(work / "log")
        q.save(arch, row)
        if (work / "spawns").is_file():
            row["actions"] = actions(work / "spawns", arch, targets)
        for target in targets.values():
            target["outcome"] = classify(target, configs, cpu)
            if target["outcome"] == "PASSED":
                cases = target["results"][0]["xml"]["named"]["passed"]
                target["exact_original_cases"] = len(cases) == len(names) and set(cases) == names
                if not (target["exact_original_cases"] and target["actions"][0]["both_tool_args"]):
                    target["outcome"] = "INCOMPLETE"
        row["outcomes"] = dict(Counter(t["outcome"] for t in targets.values()))
        row["named_case_counts"] = dict(sum((Counter({k: len(v) for k,v in r.get("xml", {}).get("named", {}).items()})
                                             for t in targets.values() for r in t["results"]), Counter()))
        e = row["events"]
        a = row.get("actions", {}).get("runners", {})
        if (child.returncode == e.get("exit") == 0 and e.get("invocation") == invocation and not e["malformed_lines"]
                and not (a.get("wrong_execution") or a.get("unexpected_test") or a.get("failed_actions") or e.get("unexpected_test"))
                and all(t["outcome"] == "PASSED" for t in targets.values())):
            row["status"] = "PASS"
        elif child.returncode not in (0, 124, 137) or any(t["outcome"] == "FAILED" for t in targets.values()):
            row["status"] = "FAIL"
    except (OSError, AssertionError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        row["collection_error"] = fault(exc)
    finally:
        row["verified"] = q.now()
        q.save(arch, row)
    return row["status"] == "PASS"


if __name__ == "__main__":
    arg, = sys.argv[1:]
    if arg == "verdict":
        results = {arch: json.loads((q.OUT / (arch + ".json")).read_text()) for arch in ("amd64", "arm64")
                   if (q.OUT / (arch + ".json")).exists()}
        statuses = {arch: r["status"] for arch, r in results.items()}
        good = statuses == {"amd64": "PASS", "arm64": "PASS"}
        q.save("verdict", {"source": PRODUCT, "supplier": SUPPLIER, "carrier": os.environ["GITHUB_SHA"], "baseline_artifact": ORIGINAL,
            "requested_per_arch": 1, "owner": OWNER, "statuses": statuses,
            "outcomes": {arch: r.get("outcomes") for arch,r in results.items()},
            "native_exits": {arch: r.get("native_exit") for arch,r in results.items()},
            "cases": {arch: r.get("named_case_counts") for arch,r in results.items()}, "pass": good, "verified_at": q.now()})
    else:
        assert arg in ("amd64", "arm64")
        good = phase(arg)
    raise SystemExit(0 if good else 1)
