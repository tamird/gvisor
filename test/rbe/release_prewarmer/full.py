"""Execute the eligible runsc tests from the original paired build ledger."""

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
import native_artifacts as native


PRODUCT = "f35d725669ae45e7617cf66e7ceee2f0f6307490"
VERSION_PRODUCT = "89b55b4b66c190f84e0ce260b6d5e780499d7b0a"
SUPPLIER = "c2f5f79dce24877679cede14b85666b7ca65c6de"
LEDGER = "c1292def802c1399a0830504f0fb67a0df56f8191c00110f3619c96a82995920"
SELECTION = "b77b1e6827e82cfc65fe972b50d8646727f14e4f0ade68a5f57fc9862d961c35"
WRAPPERS = "c7ff4346ac9d3bf6a036a9b51f0d03be912a3cac63f18614408561881f1f1049"
EXCLUDED = {
    "//runsc/container:boot_test": "requires-kvm tag; no KVM device in the worker contract",
    "//runsc/container:container_test": "requires-kvm tag; no KVM device in the worker contract",
}
NETWORK = {"//runsc/boot/portforward:portforward_test": ["requires-net:ipv4", "requires-net:loopback"]}
BAZEL = q.TMP / "bin" / "bazel"
PROJECTION = """
if .spawn then .spawn |
{m:.mnemonic, r:.runner, c:(.cacheHit//false), s:((.status//"")!=""), e:(.exitCode//0),
 p:[.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]} +
(if .mnemonic=="TestRunner" then {l:.targetLabel,x:.args[0],t:.args[1],d:.digest}
 elif ((.exitCode//0)!=0 or (.status//"")!="") then {l:.targetLabel} else {} end)
else empty end
"""


def requested() -> dict:
    raw = (q.TMP / "runsc-ledger.zip").read_bytes()
    assert len(raw) == 30623 and hashlib.sha256(raw).hexdigest() == LEDGER
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        selection = json.loads(archive.read("selection.json"))
    records = [r for r in selection["current_records"] if r[2] in ("go_test rule", "sh_test rule")]
    assert hashlib.sha256(json.dumps(records, separators=(",", ":")).encode()).hexdigest() == SELECTION
    assert Counter(r[2] for r in records) == {"go_test rule": 41, "sh_test rule": 1}
    assert {r[0] for r in records if "requires-kvm" in r[3]} == set(EXCLUDED)
    assert {r[0]: r[3] for r in records if any(t.startswith("requires-net:") for t in r[3])} == NETWORK
    assert [r[0] for r in records if r[2] == "sh_test rule"] == ["//runsc:version_test"]
    for path, blob in selection["source_build_blobs"]:
        assert path.startswith("runsc/") and q.git("rev-parse", "HEAD:" + path) == blob
    return {label: {"kind": kind, "tags": tags} for label, aspect, kind, tags in records if label not in EXCLUDED}


def stock_wrapper(arch: str) -> dict:
    raw = (q.TMP / "wrapper-ledger.zip").read_bytes()
    assert len(raw) == 29245 and hashlib.sha256(raw).hexdigest() == WRAPPERS
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        prior = json.loads(archive.read(arch + ".json"))
    assert prior["source"] == PRODUCT
    wrapper = prior["wrapper"]
    assert wrapper["choice"] == "stock" and wrapper["qualified"] and wrapper["stock_hash_matches"]
    assert wrapper["actual_self_contained"]
    return wrapper["actual_elf"]


def identity(label: str) -> str:
    return label.removeprefix("@@").removeprefix("@")


def actions(path: Path, arch: str, targets: dict, wrapper: dict) -> dict:
    platform = {"Arch": arch, "OSFamily": "linux", "container-image": "docker://gcr.io/distroless/static-debian13@" + q.PIN[arch],
                "recycle-runner": "false", "workload-isolation-type": "oci"}
    counts, failures = Counter(), []
    wrapper_paths = set()
    with subprocess.Popen([str(q.TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        with subprocess.Popen(["jq", "-c", PROJECTION], stdin=decoder.stdout, stdout=subprocess.PIPE, text=True) as rows:
            decoder.stdout.close()
            for line in rows.stdout:
                a = json.loads(line)
                native_worker = {p["name"]: p["value"] for p in a["p"]} == platform
                counts[a["r"]] += 1
                if a["r"] not in ("remote", "remote cache hit") or (not a["c"] and not native_worker):
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
                    # The native wrapper can launch a second TestRunner spawn solely
                    # to generate XML. It is not another product test execution.
                    if a["t"] == "--generate-xml":
                        targets[label]["xml_generators"].append({"digest": a["d"], "exit": a["e"], "matched_platform": native_worker})
                        continue
                    x = a["x"]
                    valid = (re.fullmatch(r"external/bazel_tools/tools/test/posix_tw", x) if arch == "amd64" else
                             re.fullmatch(r"bazel-out/[A-Za-z0-9_.+-]+/bin/external/bazel_tools/tools/test/compiled_posix_test_wrapper", x))
                    if valid:
                        wrapper_paths.add(x)
                    targets[label]["actions"].append({
                        "wrapper_path": x if valid else None,
                        "digest": a["d"], "exit": a["e"], "status_nonempty": a["s"],
                        "runner": a["r"], "cache_hit": a["c"], "matched_platform": native_worker,
                        "native_wrapper": bool(valid),
                        "owner_executable": a["t"].startswith(package + "/") and Path(a["t"]).name == name,
                    })
            assert rows.wait() == 0
        assert decoder.wait() == 0
    wrapper["actual_paths"] = sorted(wrapper_paths)
    if len(wrapper_paths) == 1:
        try:
            actual = native.elf((Path("bazel-out").resolve(strict=True).parent / next(iter(wrapper_paths))).resolve(strict=True), arch)
            wrapper["actual_elf"] = actual
            wrapper["actual_self_contained"] = not (actual["interpreter"] or actual["needed"])
            wrapper["stock_hash_matches"] = actual["sha256"] == wrapper["stock"]["sha256"]
        except (OSError, KeyError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
            wrapper["actual_elf_error"] = fault(exc)
    return {"runners": dict(counts), "failures": failures, "expected_platform": platform}


def case_name(value: str) -> str:
    # Only case identifiers, never diagnostics, skipped reasons or XML content.
    if len(value) <= 400 and value.isascii() and value.isprintable():
        return value
    return "case-sha256:" + hashlib.sha256(value.encode()).hexdigest()


def diagnostics(text):
    text = text.lower()
    return [t for t in ("fork/exec", "exec format error", "executable file not found", "no such file or directory",
                        "permission denied", "network is unreachable", "address family not supported",
                        "operation not permitted", "connection refused", "fatal error", "panic:",
                        "kernel does not support", "not supported", "timed out") if t in text]


def test_output(file: dict, label: str, row: dict, basename: str, cap: int):
    """Only read the XML advertised by this TestResult. Bind any fallback to its CAS digest."""
    assert basename in ("test.log", "test.xml") and file["name"] == basename
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
        candidates = [p for p in root.glob("*/testlogs/" + owner_path + "/" + basename)
                      if p.is_file() and p.stat().st_size == size <= cap and hashlib.sha256(p.read_bytes()).hexdigest() == digest]
        assert candidates
        path = candidates[0]
        via = "MATERIALIZED_CAS"
    path = path.resolve(strict=True)
    rel = path.relative_to(root)
    assert rel.parts[1:] == ("testlogs", *owner_path.split("/"), basename)
    assert path.is_file() and 0 < path.stat().st_size <= cap
    raw = path.read_bytes()
    h = hashlib.sha256(raw).hexdigest()
    assert (not digest or h == digest) and (size is None or len(raw) == size)
    row.update(via=via, sha256=h, bytes=len(raw))
    return path, raw


def xml_cases(file: dict, label: str, row: dict) -> None:
    path, raw = test_output(file, label, row, "test.xml", 8 * 1024 * 1024)
    counts = report.xml_cases(path)
    groups = {k: [] for k in ("passed", "failed", "skipped")}
    wrapper_cases = {k: [] for k in groups}
    generic = 0
    tree = ET.fromstring(raw)
    row["failure_categories"] = {}
    for case in tree.iter("testcase"):
        name = case.get("name", "")
        kind = ("failed" if case.find("failure") is not None or case.find("error") is not None else
                "skipped" if case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed" else "passed")
        if re.match(r"^(Test|Example|Fuzz)", name):
            groups[kind].append(case_name(name))
        else:
            generic += 1
            wrapper_cases[kind].append({"name": case_name(name), "classname": case_name(case.get("classname", ""))})
        if kind == "failed" and len(row["failure_categories"]) < 80:
            row["failure_categories"][case_name(name)] = diagnostics(" ".join(
                list(case.itertext()) + [v for item in case.iter() for v in item.attrib.values()]))
    row.update(counts={k: counts[k] for k in groups}, named=groups, unnamed=generic, wrapper_cases=wrapper_cases)
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
                if entry["status"] != "PASSED":
                    log = entry["failure_log"] = {}
                    try:
                        file, = [f for f in result.get("testActionOutput", []) if f.get("name") == "test.log"]
                        _, raw = test_output(file, label, log, "test.log", 2 * 1024 * 1024)
                        log["categories"] = diagnostics(raw.decode(errors="replace"))
                    except (OSError, ValueError, KeyError, AssertionError) as exc:
                        log["error"] = fault(exc)
            if (action := event.get("action")) and not action.get("success") and len(data["failures"]) < 16:
                data["failures"].append({"owner": ident.get("actionCompleted", {}).get("label"), "type": action.get("type"),
                    "codes": [v["code"] for v in action.get("failureDetail", {}).values() if isinstance(v, dict) and "code" in v]})
            if "aborted" in event:
                aborted = {"owner": next((v.get("label") for v in ident.values() if isinstance(v, dict)), None),
                           "reason": event["aborted"].get("reason")}
                if len(data["aborted"]) < 16:
                    data["aborted"].append(aborted)
                if aborted["owner"] and identity(aborted["owner"]) in targets:
                    targets[identity(aborted["owner"])]["aborted"].append(aborted["reason"])
    return data


def fault(exc: Exception) -> dict:
    return {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}


def classify(t: dict, configs: dict, cpu: str) -> str:
    match t:
        case {"results": [r], "actions": [a], "completed": [b], "summaries": [s]}:
            pass
        case {"results": [], "actions": []}:
            return "INCOMPATIBLE" if "INCOMPATIBLE_TARGET" in t["aborted"] else "NOT_EXECUTED"
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
    if t["kind"] == "sh_test rule":
        return "PASSED" if xml["counts"] == {"passed": 1, "failed": 0, "skipped": 0} else "INCOMPLETE"
    if not names["passed"]:
        return "ALL_SKIPPED" if names["skipped"] else "NO_NAMED_CASES"
    if xml["unnamed"]:
        return "PARTIAL_CASE_NAMES"
    return "PASSED_WITH_SKIPS" if names["skipped"] else "PASSED"


def phase(arch: str, version_only: bool = False) -> bool:
    name = arch + ("-version" if version_only else "")
    source = VERSION_PRODUCT if version_only else PRODUCT
    work = q.TMP / name
    work.mkdir()
    selection = {"//runsc:version_test": {"kind": "go_test rule", "tags": []}} if version_only else requested()
    labels = sorted(selection)
    invocation = str(uuid.uuid4())
    targets = {label: selection[label] | {k: [] for k in ("actions", "xml_generators", "results", "summaries", "completed", "aborted")} for label in labels}
    row = {"arch": arch, "source": source, "supplier": SUPPLIER, "scope": "version replacement" if version_only else "original runsc selection",
           "prior_build_artifact": 13406282574, "prior_wrapper_artifact": 13413349620,
           "prior_zip_sha256": LEDGER, "test_selection_sha256": SELECTION, "original_tests": 42,
           "excluded": {} if version_only else EXCLUDED, "network_requirements": {} if version_only else NETWORK, "requested_count": len(labels),
           "targets": targets, "status": "INCOMPLETE", "requested_invocation": invocation, "started": q.now()}
    q.save(name, row)
    cpu = {"amd64": "k8", "arm64": "aarch64"}[arch]
    try:
        assert q.git("rev-parse", "HEAD") == source and not q.git("diff", "--name-only", "HEAD")
        if version_only:
            assert q.git("rev-parse", "HEAD^") == PRODUCT
            assert q.git("diff", "--name-only", "HEAD^", "HEAD").splitlines() == ["runsc/BUILD", "runsc/version_test.go", "runsc/version_test.sh"]
        else:
            assert q.git("rev-parse", "HEAD:runsc") == "3100c9e41c8a1d999a27ed3ddde213b7d91ea069"
        platform = "//test/rbe/release_prewarmer:" + arch
        flags = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
                 "--extra_execution_platforms=" + platform, "--host_platform=" + platform,
                 "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
                 "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
                 "--extra_toolchains=//test/rbe/protoc_check:static_python" + ("_arm64" if arch == "arm64" else ""),
                 "--noincompatible_exclude_starlark_flags_from_exec_config",
                 "--lockfile_mode=off", "--jobs=32", "--color=no", "--curses=no"]
        wrapper = row["wrapper"] = {"choice": "stock", "worker": native.image(arch)}
        wrapper["stock"] = stock_wrapper(arch)
        q.save(name, row)
        flags += [
                 "--incompatible_use_native_posix_test_wrapper", "--nocache_test_results", "--test_env=GO_TEST_WRAP_TESTV=1",
                 "--runs_per_test=1", "--flaky_test_attempts=1", "--build_tag_filters=-network_plugins",
                 "--keep_going",
                 "--remote_download_outputs=toplevel", "--remote_accept_cached",
                 "--remote_download_regex=^bazel-out/[^/]+/(testlogs/runsc/.+/test[.](xml|log)|bin/external/bazel_tools/tools/test/compiled_posix_test_wrapper)$",
                 "--nobuild_event_json_file_path_conversion", "--build_event_publish_all_actions",
                 "--invocation_id=" + invocation, "--build_event_json_file=" + str(work / "events"),
                 "--execution_log_compact_file=" + str(work / "spawns")]
        if version_only:
            flags.append("--test_filter=^TestVersion$")
        with (work / "log").open("wb") as out:
            child = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "600" if version_only else "1800", str(BAZEL), *q.PROOF,
                                    "test", *flags, *labels], stdout=out, stderr=subprocess.STDOUT)
        row.update(native_exit=child.returncode, returned=q.now())
        if child.returncode not in (0, 124, 137):
            row["status"] = "FAIL"
        q.save(name, row)
        configs = {}
        row["events"] = events(work / "events", targets, configs)
        row["diagnostics"] = q.diagnostics(work / "log")
        q.save(name, row)
        if (work / "spawns").is_file():
            row["actions"] = actions(work / "spawns", arch, targets, wrapper)
        for target in targets.values():
            target["outcome"] = classify(target, configs, cpu)
        row["outcomes"] = dict(Counter(t["outcome"] for t in targets.values()))
        row["named_case_counts"] = dict(sum((Counter({k: len(v) for k,v in r.get("xml", {}).get("named", {}).items()})
                                             for t in targets.values() for r in t["results"]), Counter()))
        e = row["events"]
        a = row.get("actions", {}).get("runners", {})
        wrapper["qualified"] = bool(wrapper.get("actual_self_contained") and wrapper.get("stock_hash_matches"))
        version_verified = True
        if version_only:
            version_verified = False
            try:
                status = Path("bazel-out/stable-status.txt").read_bytes()
                assert len(status) <= 4096
                versions = [line.split(maxsplit=1)[1] for line in status.decode().splitlines() if line.startswith("STABLE_VERSION ")]
                version, = versions
                assert re.fullmatch(r"[A-Za-z0-9._+-]{1,120}", version)
                expected = q.git("describe", "--always", "--tags", "--exclude", "release-*-staging", "--abbrev=12", "--dirty")
                row["workspace_status"] = {"version": version, "sha256": hashlib.sha256(status).hexdigest(), "matches_git_describe": version == expected}
                version_verified = version == expected and any(
                    r.get("xml", {}).get("named") == {"passed": ["TestVersion"], "failed": [], "skipped": []}
                    for r in targets["//runsc:version_test"]["results"])
            except (OSError, AssertionError, ValueError, subprocess.SubprocessError) as exc:
                row["workspace_status"] = {"error": fault(exc)}
            row["version_case_verified"] = version_verified
        if (wrapper["qualified"] and version_verified and child.returncode == e.get("exit") == 0 and e.get("invocation") == invocation and not e["malformed_lines"]
                and not (a.get("wrong_execution") or a.get("unexpected_test") or a.get("failed_actions") or e.get("unexpected_test"))
                and all(t["outcome"] in ("PASSED", "PASSED_WITH_SKIPS") for t in targets.values())):
            row["status"] = "PASS"
        elif child.returncode not in (0, 124, 137) or any(t["outcome"] == "FAILED" for t in targets.values()):
            row["status"] = "FAIL"
    except (OSError, AssertionError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        row["collection_error"] = fault(exc)
    finally:
        row["verified"] = q.now()
        q.save(name, row)
    return row["status"] == "PASS"


if __name__ == "__main__":
    arg, = sys.argv[1:]
    if arg == "verdict":
        results = {arch: json.loads((q.OUT / (arch + ".json")).read_text()) for arch in ("amd64", "arm64", "amd64-version", "arm64-version")
                   if (q.OUT / (arch + ".json")).exists()}
        statuses = {arch: results[arch]["status"] for arch in ("amd64", "arm64") if arch in results}
        version_statuses = {arch: results[arch + "-version"]["status"] for arch in ("amd64", "arm64") if arch + "-version" in results}
        original_pass = statuses == {"amd64": "PASS", "arm64": "PASS"}
        version_pass = version_statuses == {"amd64": "PASS", "arm64": "PASS"}
        good = original_pass and version_pass
        q.save("verdict", {"source": PRODUCT, "supplier": SUPPLIER, "carrier": os.environ["GITHUB_SHA"], "prior_build": 13406282574,
            "original_tests": 42, "requested_per_arch": 40, "requested_go_tests": 39, "requested_shell_tests": 1,
            "excluded": EXCLUDED, "network_requirements": NETWORK, "statuses": statuses,
            "version_source": VERSION_PRODUCT, "version_statuses": version_statuses, "original_pass": original_pass, "version_pass": version_pass,
            "outcomes": {arch: r.get("outcomes") for arch,r in results.items()},
            "native_exits": {arch: r.get("native_exit") for arch,r in results.items()},
            "cases": {arch: r.get("named_case_counts") for arch,r in results.items()}, "pass": good, "verified_at": q.now()})
    else:
        assert arg in ("amd64", "arm64", "amd64-version", "arm64-version")
        good = phase(arg.removesuffix("-version"), arg.endswith("-version"))
    raise SystemExit(0 if good else 1)
