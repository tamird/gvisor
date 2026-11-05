"""Qualify the declared boot path after an incomplete checkout preflight."""

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


BASE = "f35d725669ae45e7617cf66e7ceee2f0f6307490"
SUPPLIER = "c2f5f79dce24877679cede14b85666b7ca65c6de"
ORIGINAL = "4c129e177c233c650b7c20a6671a1b385d2ec924a74d853c0dad0110cf550025"
WRAPPERS = "c7ff4346ac9d3bf6a036a9b51f0d03be912a3cac63f18614408561881f1f1049"
BOOT = "9ccd97431e05ba31f7dd7754b68335513c8d422a"
BOOT_TARGET = "//runsc/boot:boot_test"
APP = "//test/cmd/test_app:test_app"
BOOT_CASES = ["TestRun", "TestStartupHeapReleaseOnStart"]
PHASES = {
    "boot-named": {"source": BOOT, "parent": BASE, "proposal": "2289dbad7f76a19617714a30124906dc05a69f85",
                   "target": BOOT_TARGET, "cases": BOOT_CASES, "seconds": 480,
                   "paths": ["runsc/boot/BUILD", "runsc/boot/loader_test.go", "test/cmd/test_app/BUILD"]},

}
PHASES["boot-full"] = PHASES["boot-named"] | {"cases": [], "seconds": 180}
BOOT_FILES = ["compat_test.go", "fscheckpoint_test.go", "lifecyclecpus_test.go", "loader_test.go",
              "mount_hints_test.go", "restore_test.go", "seccomp_test.go", "vfs_test.go"]

BAZEL = q.TMP / "bin" / "bazel"
PROJECTION = """
if .spawn then .spawn |
{m:.mnemonic, r:.runner, c:(.cacheHit//false), s:((.status//"")!=""), e:(.exitCode//0),
 p:[.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]} +
(if .mnemonic=="TestRunner" then {l:.targetLabel,x:.args[0],t:.args[1],d:.digest}
 elif ((.exitCode//0)!=0 or (.status//"")!="") then {l:.targetLabel} else {} end)
else empty end
"""


def original(arch: str, phase_name: str) -> dict:
    raw = (q.TMP / "original.zip").read_bytes()
    assert len(raw) == 21514 and hashlib.sha256(raw).hexdigest() == ORIGINAL
    scope = "boot-full" if phase_name == "boot-full" else "boot-named"
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        prior = json.loads(archive.read(arch + "-" + scope + ".json"))
    assert prior["source"] == "cc47a1a5856bbfc2c42cf52add1aebf49eb509d5" and prior["supplier"] == SUPPLIER
    assert prior["native_exit"] == 3 and prior["status"] == "FAIL"
    target = prior["targets"][BOOT_TARGET]
    result, = target["results"]
    assert target["outcome"] == "FAILED" and target["fresh_native"]
    assert target["cpu"] == {"amd64": "k8", "arm64": "aarch64"}[arch]
    xml = result["xml"]
    assert xml["counts"] == ({"passed": 66, "failed": 13, "skipped": 0} if scope == "boot-full" else
                              {"passed": 0, "failed": 2, "skipped": 0})
    assert set(BOOT_CASES) <= set(xml["named"]["failed"])
    return {"artifact": 13422210533, "zip_sha256": ORIGINAL, "source": prior["source"],
            "scope": "prior guest-path failure",
            "invocation": prior["events"]["invocation"], "outcome": target["outcome"],
            "xml_sha256": xml["sha256"], "counts": xml["counts"], "failed": xml["named"]["failed"]}


def previous_preflight(arch: str, scope: str) -> dict:
    raw = (q.TMP / "previous-preflight.zip").read_bytes()
    digest = "492d910191abf432544eb365e240f9d36d16bc4b7813f268da9ea56e6d27f303"
    assert len(raw) == 12702 and hashlib.sha256(raw).hexdigest() == digest
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        prior = json.loads(archive.read(arch + "-" + scope + ".json"))
    assert prior["source"] == BOOT and prior["status"] == "INCOMPLETE"
    rows = list(prior["modes"].values()) if scope == "boot-fixture" else [prior]
    for row in rows:
        assert row["collection_error"]["type"] == "CalledProcessError"
        assert "native_exit" not in row
        assert "events" not in row
        assert all(not t["results"] and not t["actions"] for t in row.get("targets", {}).values())
    return {"artifact": 13425788812, "zip_sha256": digest, "source": BOOT,
            "outcome": "INCOMPLETE_BEFORE_BAZEL", "scopes": len(rows),
            "cause": "shallow checkout omitted expected parent"}


def source(phase_name):
    meta = PHASES[phase_name]
    assert q.git("rev-parse", "HEAD") == meta["source"] and not q.git("diff", "--name-only", "HEAD")
    assert q.git("rev-parse", "HEAD^") == meta["parent"]
    assert q.git("diff", "--name-only", "HEAD^", "HEAD").splitlines() == meta["paths"]
    assert q.git("rev-parse", "HEAD:tools/rules_go_bootstrap_shell.patch") == "033fde5646ef43acaecae9b033ec8f867200e8c1"
    assert '"GVISOR_BOOT_TEST_APP": "$(rootpath //test/cmd/test_app)"' in q.git("show", "HEAD:runsc/boot/BUILD")


def common_flags(arch):
    platform = "//test/rbe/release_prewarmer:" + arch
    return ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
            "--extra_execution_platforms=" + platform, "--host_platform=" + platform,
            "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
            "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
            "--extra_toolchains=//test/rbe/protoc_check:static_python" + ("_arm64" if arch == "arm64" else ""),
            "--noincompatible_exclude_starlark_flags_from_exec_config", "--lockfile_mode=off",
            "--jobs=32", "--color=no", "--curses=no"]


def boot_roots():
    names = []
    for file in BOOT_FILES:
        names += re.findall(r"^func (Test\w+)\(t \*testing.T\)", q.git("show", "HEAD:runsc/boot/" + file), re.M)
    assert len(names) == len(set(names)) and set(BOOT_CASES) <= set(names)
    return sorted(names)


def stock_wrapper(arch: str) -> dict:
    raw = (q.TMP / "wrapper-ledger.zip").read_bytes()
    assert len(raw) == 29245 and hashlib.sha256(raw).hexdigest() == WRAPPERS
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        prior = json.loads(archive.read(arch + ".json"))
    assert prior["source"] == BASE
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
                        text = raw.decode(errors="replace")
                        log["categories"] = diagnostics(text)
                        log["unreleased_stdio_fds"] = sorted(set(map(int, re.findall(
                            r"F_GETFD on sandbox stdio FD (25[3-5]) after failed exec: got <nil>, want EBADF", text))))
                        log["missing_guest_enoent"] = "Error opening /missing: no such file or directory" in text
                        log["boot_faults"] = [tag for tag, phrase in (
                            ("test_app_permission_denied", "failed to load /test_app: permission denied"),
                            ("reserved_stdio_busy", "FD 253 is already in use"),
                            ("missing_declared_app_env", "GVISOR_BOOT_TEST_APP is not set"),
                            ("nonregular_declared_app", "is not a regular file")) if phrase in text]
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


def phase(arch: str, phase_name: str) -> bool:
    name = arch + "-" + phase_name
    meta = PHASES[phase_name]
    target_label = meta["target"]
    expected_cases = boot_roots() if phase_name == "boot-full" else meta["cases"]
    work = q.TMP / name
    work.mkdir()
    selection = {target_label: {"kind": "go_test rule", "tags": []}}
    labels = sorted(selection)
    invocation = str(uuid.uuid4())
    targets = {label: selection[label] | {k: [] for k in ("actions", "xml_generators", "results", "summaries", "completed", "aborted")} for label in labels}
    row = {"arch": arch, "source": meta["source"], "proposal": meta["proposal"], "supplier": SUPPLIER,
           "scope": phase_name, "expected_cases": expected_cases,
           "prior_wrapper_artifact": 13413349620, "requested_count": len(labels),
           "targets": targets, "status": "INCOMPLETE", "requested_invocation": invocation, "started": q.now()}
    q.save(name, row)
    cpu = {"amd64": "k8", "arm64": "aarch64"}[arch]
    try:
        source(phase_name)
        row["original"] = original(arch, phase_name)
        row["previous_preflight"] = previous_preflight(arch, phase_name)
        flags = common_flags(arch)
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
        if meta["cases"]:
            flags.append("--test_filter=^(" + "|".join(meta["cases"]) + ")$")
        with (work / "log").open("wb") as out:
            child = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", str(meta["seconds"]), str(BAZEL), *q.PROOF,
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
        cases_verified = False
        results = targets[target_label]["results"]
        if len(results) == 1 and (xml := results[0].get("xml", {})).get("named"):
            names = xml["named"]
            all_cases = names["passed"] + names["failed"] + names["skipped"]
            if phase_name == "boot-full":
                cases_verified = (sorted({n.split("/")[0] for n in all_cases}) == expected_cases
                                  and not names["failed"] and not xml["unnamed"])
            else:
                cases_verified = (sorted(all_cases) == expected_cases and not names["failed"]
                                  and not names["skipped"] and not xml["unnamed"])
        row["selected_cases_verified"] = cases_verified
        if (wrapper["qualified"] and cases_verified and child.returncode == e.get("exit") == 0 and e.get("invocation") == invocation and not e["malformed_lines"]
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


FIXTURE_EXPR = """json.encode({
  "label": str(target.label),
  "cpu": build_options(target)["//command_line_option:cpu"],
  "race": {k: v for k, v in build_options(target).items() if k.endswith("//go/config:race")},
  "files": [f.path for f in target.files.to_list()],
  "runfiles": [{"path": f.path, "short_path": f.short_path} for f in providers(target)["DefaultInfo"].default_runfiles.files.to_list() if "/bin/test/cmd/test_app/" in f.path]
})"""
FIXTURE_SELECT = r"""
if .spawn then .spawn |
{runner,mnemonic,cache:(.cacheHit//false),exit:(.exitCode//0),status:((.status//"")!=""),
 platform:[.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]} +
(if .mnemonic=="GoLink" and (.targetLabel|test("^(@@?)?//test/cmd/test_app:")) then
 {label:.targetLabel,digest,outputs,race:(.args|index("-race")!=null)} else {} end)
elif .file and (.file.path|contains("/bin/test/cmd/test_app/")) then
 {file:{id,path:.file.path,digest:.file.digest}}
else empty end
"""


def fixture_actions(path, arch):
    expected = {"Arch": arch, "OSFamily": "linux", "container-image": "docker://gcr.io/distroless/static-debian13@" + q.PIN[arch],
                "recycle-runner": "false", "workload-isolation-type": "oci"}
    counts, files, links = Counter(), {}, []
    with subprocess.Popen([str(q.TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        with subprocess.Popen(["jq", "-c", FIXTURE_SELECT], stdin=decoder.stdout, stdout=subprocess.PIPE, text=True) as rows:
            decoder.stdout.close()
            for line in rows.stdout:
                item = json.loads(line)
                if "file" in item:
                    f = item["file"]
                    files[f["id"]] = {"path": f["path"], "digest": f["digest"]}
                    continue
                props = {p["name"]: p["value"] for p in item.pop("platform")}
                counts[item["runner"]] += 1
                if item["runner"] not in ("remote", "remote cache hit") or (not item["cache"] and props != expected):
                    counts["wrong_execution"] += 1
                if item["exit"] or item["status"]:
                    counts["failed_actions"] += 1
                if item["mnemonic"] == "TestRunner":
                    counts["TestRunner"] += 1
                if "label" in item:
                    item["worker_matches"] = props == expected
                    links.append(item)
            assert rows.wait() == 0
        assert decoder.wait() == 0
    for link in links:
        link["outputs"] = [files[o["outputId"]] for o in link.pop("outputs", []) if o.get("outputId") in files]
    return {"runners": dict(counts), "links": links}


def fixture(arch):
    name = arch + "-boot-fixture"
    row = {"source": BOOT, "arch": arch, "scope": "build-only fixture ELF and declared boot runfiles",
           "status": "INCOMPLETE", "modes": {}, "started": q.now()}
    q.save(name, row)
    for mode in ("normal", "race"):
        result = row["modes"][mode] = {"status": "INCOMPLETE", "started": q.now()}
        work = q.TMP / (name + "-" + mode)
        work.mkdir()
        try:
            source("boot-named")
            row["previous_preflight"] = previous_preflight(arch, "boot-fixture")
            invocation = str(uuid.uuid4())
            result["requested_invocation"] = invocation
            flags = common_flags(arch) + (["--config=race"] if mode == "race" else [])
            flags += ["--keep_going", "--remote_accept_cached", "--remote_download_outputs=toplevel",
                      "--nobuild_event_json_file_path_conversion", "--build_event_publish_all_actions"]
            with (work / "log").open("wb") as log:
                child = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "90" if mode == "normal" else "600",
                    str(BAZEL), *q.PROOF, "build", *flags, "--invocation_id=" + invocation,
                    "--build_event_json_file=" + str(work / "events"), "--execution_log_compact_file=" + str(work / "spawns"), APP],
                    stdout=log, stderr=subprocess.STDOUT)
            result.update(native_exit=child.returncode, returned=q.now(), diagnostics=q.diagnostics(work / "log"))
            if child.returncode not in (0, 124, 137):
                result["status"] = "FAIL"
            q.save(name, row)
            targets = {APP: {k: [] for k in ("results", "summaries", "completed", "aborted")}}
            configs = {}
            result["events"] = events(work / "events", targets, configs)
            result["owner"] = targets[APP]
            if (work / "spawns").is_file():
                result["actions"] = fixture_actions(work / "spawns", arch)
            q.save(name, row)
            assert child.returncode == result["events"].get("exit") == 0
            assert result["events"].get("invocation") == invocation and not result["events"]["malformed_lines"]
            assert not result["events"].get("unexpected_test") and not targets[APP]["results"]
            completed, = targets[APP]["completed"]
            assert completed["built"] and configs[completed["config"]] == {"amd64": "k8", "arm64": "aarch64"}[arch]
            runners = result["actions"]["runners"]
            assert not any(runners.get(k) for k in ("wrong_execution", "failed_actions", "TestRunner"))
            query_id = str(uuid.uuid4())
            query = 'filter("^(@@?)?//(runsc/boot:boot_test|test/cmd/test_app:test_app)$", deps(//runsc/boot:boot_test, 1))'
            with (work / "providers").open("wb") as out, (work / "query-log").open("wb") as log:
                child = subprocess.run(["timeout", "--signal=INT", "--kill-after=10", "60", str(BAZEL), *q.PROOF,
                    "cquery", *flags, "--invocation_id=" + query_id, "--build_event_json_file=" + str(work / "query-events"),
                    "--output=starlark", "--starlark:expr=" + FIXTURE_EXPR, query], stdout=out, stderr=log)
            result["cquery_exit"] = child.returncode
            result["cquery_diagnostics"] = q.diagnostics(work / "query-log")
            result["cquery_events"] = events(work / "query-events", {}, {})
            q.save(name, row)
            qe = result["cquery_events"]
            assert child.returncode == qe.get("exit") == 0 and qe.get("invocation") == query_id
            assert not (qe.get("unexpected_test") or qe["failures"] or qe["malformed_lines"])
            assert all(x["reason"] == "NO_BUILD" for x in qe["aborted"])
            assert (work / "providers").stat().st_size <= 64 * 1024
            providers = [json.loads(line) for line in (work / "providers").read_text().splitlines()]
            assert len(providers) == 2
            providers = {identity(p["label"]): p for p in providers}
            assert set(providers) == {APP, BOOT_TARGET}
            assert all(p["cpu"] == {"amd64": "k8", "arm64": "aarch64"}[arch] for p in providers.values())
            if mode == "race":
                assert all(list(p["race"].values()) == [True] for p in providers.values())
            else:
                assert all(not any(p["race"].values()) for p in providers.values())
            result["declared_providers"] = providers
            app, = providers[APP]["files"]
            assert re.fullmatch(r"bazel-out/[A-Za-z0-9_.+-]+/bin/test/cmd/test_app/[A-Za-z0-9_./+-]+", app)
            assert ".." not in Path(app).parts
            runfile, = [f for f in providers[BOOT_TARGET]["runfiles"] if f["path"] == app]
            assert runfile["short_path"] == app.split("/bin/", 1)[1]
            assert Path(runfile["short_path"]).name == "test_app"
            path = Path(app).resolve(strict=True)
            assert path.is_relative_to(Path("bazel-out").resolve(strict=True))
            binary = result["elf"] = native.elf(path, arch)
            assert binary["entry"] and not binary["interpreter"] and not binary["needed"]
            result["matching_link_outputs"] = []
            for link in result["actions"]["links"]:
                for output in link["outputs"]:
                    if output["path"] == app:
                        assert output["digest"]["hash"] == binary["sha256"] and int(output["digest"]["sizeBytes"]) == binary["bytes"]
                        assert not link["exit"] and not link["status"]
                        assert link["race"] == (mode == "race")
                        result["matching_link_outputs"].append({"label": identity(link["label"]), "cache": link["cache"],
                            "race_arg": link["race"], "worker_matches": link["worker_matches"], "digest": output["digest"]})
            result["link_observation"] = "ACTION_OUTPUT_BYTES" if result["matching_link_outputs"] else "DECLARED_BUILD_OUTPUT_BYTES_NO_MATCHED_LINK"
            result["status"] = "PASS"
        except (OSError, AssertionError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            result["collection_error"] = fault(exc)
        finally:
            result["verified"] = q.now()
            q.save(name, row)
    row["status"] = "PASS" if all(r["status"] == "PASS" for r in row["modes"].values()) else "INCOMPLETE"
    row["verified"] = q.now()
    q.save(name, row)
    return row["status"] == "PASS"


if __name__ == "__main__":
    if sys.argv[1:] == ["verdict"]:
        expected = [arch + "-" + scope for scope in (*PHASES, "boot-fixture") for arch in ("amd64", "arm64")]
        results = {name: json.loads((q.OUT / (name + ".json")).read_text()) for name in expected
                   if (q.OUT / (name + ".json")).exists()}
        statuses = {name: result["status"] for name, result in results.items()}
        expected_statuses = dict.fromkeys(expected, "PASS")
        good = statuses == expected_statuses
        q.save("verdict", {"sources": {k: m["source"] for k,m in PHASES.items()}, "supplier": SUPPLIER,
            "carrier": os.environ["GITHUB_SHA"], "original_artifact": 13422210533,
            "statuses": statuses, "expected_statuses": expected_statuses, "outcomes": {name: r.get("outcomes") for name,r in results.items()},
            "native_exits": {name: r.get("native_exit") for name,r in results.items()},
            "cases": {name: r.get("named_case_counts") for name,r in results.items()}, "pass": good, "verified_at": q.now()})
    else:
        arch, scope = sys.argv[1:]
        assert arch in ("amd64", "arm64")
        good = fixture(arch) if scope == "boot-fixture" else phase(arch, scope)
    raise SystemExit(0 if good else 1)
