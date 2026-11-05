#!/usr/bin/env python3
"""Recover the failed LLVM consumer and inspect original output headers."""

import base64
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import struct
import urllib.error
import traceback
import uuid
import zipfile

import qualify as q

ORIGINAL_SHA = "bb17bf615274f393c934cec76b93a7cf28b0bf15ba5a7e28ea8233457e683362"
CENSUS_SHA = "807920320bc82ba24d8a849beb97bcca826478983e16e2395980fa3a5629f939"
ORIGINAL_SOURCE = "5388293bdd842102babe313ae951489e78a6dfae"
TARGET = "//pkg/sentry/devices/nvproxy:nvproxy_driver_parity_test"
GENERATOR = "@@llvm++llvm+llvm-project//llvm:instrumentor_variables_gen"
SLIMVM = ["//pkg/sentry/platform/slimvm:slimvm", "//pkg/sentry/platform/slimvm/testutil:testutil"]
INVOCATIONS = {"amd64": "0fcbd4b2-2d65-4e0a-b659-66f9d34cba92", "arm64": "93689dfe-a787-446b-bbe6-9882736395c8"}
SELECT = r"""
if .spawn then
  .spawn | . as $s |
  {kind:"action",runner,mnemonic,cache:(.cacheHit//false),exit:(.exitCode//0),status_nonempty:((.status//"")!=""),
   platform:[.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]} +
  (if (.mnemonic=="GoLink" and ($tests|has($s.targetLabel|sub("^@@?//";"//")))) or (.exitCode//0)!=0 or (.status//"")!="" then
    {label:.targetLabel,mnemonic,executable:.args[0],digest,outputs}
   else {} end)
elif .file and (.file.path|contains("/bin/pkg/sentry/")) then
  {kind:"file",id,path:.file.path,digest:.file.digest}
else empty end
"""


def normalized(label: str) -> str:
    return "//" + label.split("//", 1)[1] if label.startswith(("@@//", "@//")) else label


def original() -> dict:
    raw = (q.TMP / "original.zip").read_bytes()
    assert len(raw) == 51068 and hashlib.sha256(raw).hexdigest() == ORIGINAL_SHA
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert {m.filename: m.file_size for m in archive.infolist()} == {
            "amd64.json": 328469, "arm64.json": 329246, "selection.json": 137284, "verdict.json": 687}
        return {name.removesuffix(".json"): json.loads(archive.read(name)) for name in archive.namelist()}


def requested(label: str) -> str:
    return label.replace("@@llvm++llvm+llvm-project//", "@llvm-project//", 1)


def selection() -> list:
    previous = original()
    assert previous["verdict"]["source"] == ORIGINAL_SOURCE
    assert previous["selection"]["selection_sha256"] == CENSUS_SHA
    selected = [row for row in previous["selection"]["current_records"] if row[0] == TARGET]
    assert selected == [[TARGET, "", "go_test rule", []]]
    selected.append([GENERATOR, "", "expand_template rule", []])
    assert q.git("diff", "--name-only", ORIGINAL_SOURCE, q.PRODUCT) == "MODULE.bazel"
    for label, path in zip(SLIMVM, ("pkg/sentry/platform/slimvm/BUILD", "pkg/sentry/platform/slimvm/testutil/BUILD")):
        text = Path(path).read_text()
        assert '"@platforms//cpu:x86_64"' in text and 'name = "' + label.split(":")[1] + '"' in text
    q.save("selection", {"original_artifact": 13394940473, "original_zip_sha256": ORIGINAL_SHA,
                         "original_source": ORIGINAL_SOURCE, "original_status": "FAIL",
                         "original_owners": 436, "mapped_current_owners": 433,
                         "selection_sha256": CENSUS_SHA, "current_records": selected,
                         "arm64_incompatible": SLIMVM,
                         "scope": "Only the failed consumer and its template generator are rebuilt; no tests"})
    return selected


def prior_headers() -> int:
    # Reuse the existing fixed-origin, no-redirect metadata transport. The public
    # GetFileRange RPC (BuildBuddy v2.322.0) reads only the requested CAS prefix.
    import cas_inputs as cas

    result = {"original_artifact": 13394940473, "original_zip_sha256": ORIGINAL_SHA,
              "source": ORIGINAL_SOURCE, "status": "INCOMPLETE", "started": q.now(), "architectures": {},
              "scope": "Original action-output URI headers only; whole blobs are not independently hashed; no execution"}
    q.save("original-headers", result)
    try:
        previous = original()
        tests = {r[0] for r in previous["selection"]["current_records"] if r[2] == "go_test rule"}
        assert len(tests) == 93
        consecutive_errors = 0
        for arch, invocation in INVOCATIONS.items():
            prior = previous[arch]
            assert prior["source"] == ORIGINAL_SOURCE and prior["status"] == "FAIL" and "collection_error" not in prior
            assert prior["native_make_exit"] == 2 and prior["build"]["native_exit"] == 1
            assert prior["build"]["invocation"] == prior["build"]["requested_invocation"] == invocation
            assert prior["build"]["test_results"] == 0
            failed = [a for a in prior["actions"] if a["exit"] or a["status_nonempty"]]
            failure, = failed
            assert failure["label"] == GENERATOR and failure["mnemonic"] == "Genrule" and failure["executable"] == "/bin/bash"
            actions = [a for a in prior["actions"] if a["mnemonic"] == "GoLink"]
            assert len(actions) == 93 and {a["label"] for a in actions} == tests
            arm = result["architectures"][arch] = {"invocation": invocation, "original_coverage": prior["coverage"],
                "incompatible": SLIMVM if arch == "arm64" else [], "headers": {}, "errors": []}
            for action in actions:
                assert action["runner"] == "remote" and action["worker_matches"] and not action["cache"]
                assert action["exit"] == 0 and not action["status_nonempty"] and action["executable"].endswith("/builder")
                output, = action["outputs"]
                digest = output["digest"]
                assert re.fullmatch(r"[0-9a-f]{64}", digest["hash"]) and 4096 < int(digest["sizeBytes"]) <= 64 * 1024**2
                row = {"action": action["digest"], "output": digest, "path": output["path"]}
                try:
                    uri = "bytestream://remote.buildbuddy.io/blobs/" + digest["hash"] + "/" + digest["sizeBytes"]
                    response = json.loads(cas.fetch("api/v1/GetFileRange", {"uri": uri, "start": "0", "end": "4096"}))
                    data = base64.b64decode(response["data"], validate=True)
                    assert len(data) == 4096
                    elf = q.elf_bytes(data)
                    assert elf["machine"] == {"amd64": 62, "arm64": 183}[arch] and elf["load"] and elf["entry"]
                    row.update(prefix_sha256=elf.pop("sha256"), prefix_bytes=elf.pop("bytes"), elf=elf)
                    arm["headers"][action["label"]] = row
                    consecutive_errors = 0
                except (AssertionError, KeyError, ValueError, OSError, struct.error) as error:
                    arm["errors"].append({"label": action["label"], "type": type(error).__name__,
                                          "http_status": error.code if isinstance(error, urllib.error.HTTPError) else None})
                    consecutive_errors += 1
                q.save("original-headers", result)
                if consecutive_errors == 3:
                    return 1
        if all(len(row["headers"]) == 93 and not row["errors"] for row in result["architectures"].values()):
            result["status"] = "PASS"
    except (AssertionError, KeyError, ValueError, OSError, KeyboardInterrupt) as error:
        result["collection_error"] = {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
    finally:
        result["verified_at"] = q.now()
        q.save("original-headers", result)
    return int(result["status"] != "PASS")


def wrapped() -> int:
    """Reuse the whole-graph Make observer, retaining the actual Bazel exits."""
    arch = os.environ["COHORT_ARCH"]
    command, *args = sys.argv[1:]
    assert command in ("build", "cquery"), command
    assert "--build_tag_filters=-network_plugins" in args
    if command == "build":
        expected = json.loads((q.OUT / "selection.json").read_text())["current_records"]
        assert sorted(arg for arg in args if arg.startswith(("//", "@"))) == sorted(requested(row[0]) for row in expected)
    prefix = q.TMP / (arch + "-" + command)
    assert not prefix.with_suffix(".json").exists(), (arch, command)
    invocation = str(uuid.uuid4())
    row = {"command": command, "requested_invocation": invocation, "started": q.now()}
    prefix.with_suffix(".json").write_text(json.dumps(row))
    flags = ["--invocation_id=" + invocation, "--build_event_json_file=" + str(prefix.with_suffix(".bep"))]
    if command == "build":
        flags += ["--keep_going", "--build_event_publish_all_actions",
                  "--execution_log_compact_file=" + str(prefix.with_suffix(".compact"))]
    with prefix.with_suffix(".out").open("wb") as out, prefix.with_suffix(".err").open("wb") as err:
        child = subprocess.run([os.environ["REAL_BAZEL"], *q.PROOF, command, *flags, *args], stdout=out, stderr=err)
    row.update(native_exit=child.returncode, returned=q.now())
    prefix.with_suffix(".json").write_text(json.dumps(row))
    if command == "cquery":
        with prefix.with_suffix(".out").open("rb") as source:
            while chunk := source.read(1024 * 1024):
                sys.stdout.buffer.write(chunk)
    return child.returncode


def events(prefix: Path) -> dict:
    row = json.loads(prefix.with_suffix(".json").read_text())
    configured, completed, aborted, failures, configurations = [], [], [], [], {}
    file_sets, outputs = {}, []
    attempts = 0
    bep = prefix.with_suffix(".bep")
    if bep.is_file():
        with bep.open("rb") as source:
            for line in source:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if line.endswith(b"\n"):
                        raise
                    row["incomplete_tail_bytes"] = len(line)
                    break
                key = event.get("id", {})
                if "namedSetOfFiles" in event:
                    values = event["namedSetOfFiles"]
                    file_sets[key["namedSet"]["id"]] = (bool(values.get("files")), [s["id"] for s in values.get("fileSets", [])])
                if "started" in event:
                    row.update(invocation=event["started"]["uuid"], bazel=event["started"].get("buildToolVersion"))
                if "finished" in event:
                    row.update(finished=event["finished"].get("finishTime"), exit=event["finished"]["exitCode"].get("code", 0))
                if "configuration" in event:
                    configurations[key["configuration"]["id"]] = event["configuration"].get("cpu")
                if "configured" in event and "targetConfigured" in key:
                    target = key["targetConfigured"]
                    for child in event.get("children", []):
                        if (end := child.get("targetCompleted")) is not None:
                            configured.append([normalized(target["label"]), target.get("aspect", ""),
                                               event["configured"].get("targetKind"), sorted(event["configured"].get("tag", [])),
                                               end["configuration"]["id"]])
                if "targetCompleted" in key and "completed" in event:
                    target = key["targetCompleted"]
                    completed.append([normalized(target["label"]), target.get("aspect", ""),
                                      target["configuration"]["id"], bool(event["completed"].get("success"))])
                    values = event["completed"]
                    groups = [s["id"] for g in values.get("outputGroup", []) if g["name"] == "default" for s in g.get("fileSets", [])]
                    outputs.append([*completed[-1][:3], groups, len(values.get("importantOutput", []))])
                if "aborted" in event:
                    target = key.get("targetConfigured", key.get("targetCompleted", {}))
                    aborted.append({"label": normalized(target.get("label", "")), "aspect": target.get("aspect", ""),
                                    "configuration": target.get("configuration", {}).get("id"),
                                    "reason": event["aborted"].get("reason")})
                if (action := event.get("action")) is not None and not action.get("success"):
                    owner = key.get("actionCompleted", {})
                    failures.append({"owner": normalized(owner.get("label", "")), "mnemonic": action.get("type"),
                                     "exit": action.get("exitCode")})
                attempts += int("testResult" in key)
    def has_files(ids: list) -> bool | None:
        pending, seen, missing = list(ids), set(), False
        while pending:
            value = pending.pop()
            if value in seen:
                continue
            seen.add(value)
            if value not in file_sets:
                missing = True
                continue
            direct, children = file_sets[value]
            if direct:
                return True
            pending.extend(children)
        return None if missing else False
    for output in outputs:
        output[3] = has_files(output[3])
    row.update(configured=sorted(configured), completed=sorted(completed), aborted=aborted,
               failed_action_count=len(failures), failed_actions=failures[:32], configurations=configurations,
               output_metadata=outputs, test_results=attempts, diagnostics=q.diagnostics(prefix.with_suffix(".err")))
    return row


def actions(path: Path, arch: str, tests: set) -> tuple:
    expected = {"Arch": arch, "OSFamily": "linux", "workload-isolation-type": "oci", "recycle-runner": "false",
                "container-image": "docker://gcr.io/distroless/static-debian13@" + q.PIN[arch]}
    counts, files, selected = Counter(), {}, []
    with subprocess.Popen([str(q.TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as reader:
        with subprocess.Popen(["jq", "-c", "--argjson", "tests", json.dumps(dict.fromkeys(sorted(tests), True)), SELECT],
                              stdin=reader.stdout, stdout=subprocess.PIPE, text=True) as rows:
            reader.stdout.close()
            for line in rows.stdout:
                item = json.loads(line)
                if item["kind"] == "file":
                    files[item["id"]] = item
                    continue
                props = {p["name"]: p["value"] for p in item.pop("platform")}
                counts[item["runner"] or "unknown"] += 1
                if item["mnemonic"] == "TestRunner":
                    counts["TestRunner"] += 1
                if props != expected:
                    counts["different_platform"] += 1
                if "label" in item:
                    item["label"] = normalized(item["label"])
                    item["worker_matches"] = props == expected
                    if item["exit"] or item["status_nonempty"]:
                        counts["failed_actions"] += 1
                    if (item["label"] in tests and item["mnemonic"] == "GoLink") or counts["failed_actions"] <= 32:
                        selected.append(item)
            assert rows.wait() == 0
        assert reader.wait() == 0
    for item in selected:
        item["outputs"] = [files[o["outputId"]] for o in (item.pop("outputs", None) or []) if o.get("outputId") in files]
    return dict(counts), selected


def phase(arch: str, original: list) -> dict:
    expected = {row[0]: row[1:4] for row in original}
    tests = {label for label, fields in expected.items() if fields[1] == "go_test rule"}
    row = {"source": q.PRODUCT, "arch": arch, "status": "INCOMPLETE", "started": q.now(),
           "requested_count": len(expected), "selection_sha256": CENSUS_SHA}
    q.save(arch, row)
    exe = "//test/rbe/release_prewarmer:" + arch
    flags = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
             "--extra_execution_platforms=" + exe, "--host_platform=" + exe,
             "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
             "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
             "--extra_toolchains=//test/rbe/protoc_check:static_python" + ("_arm64" if arch == "arm64" else ""),
             "--remote_accept_cached", "--remote_download_outputs=toplevel", "--jobs=32",
             "--lockfile_mode=off", "--color=no", "--curses=no"]
    try:
        row["disk_free_before"] = os.statvfs(q.TMP).f_bavail * os.statvfs(q.TMP).f_frsize
        assert row["disk_free_before"] >= 20 * 1024**3
        shim = q.TMP / (arch + "-bin")
        shim.mkdir()
        (shim / "bazel").symlink_to(Path(__file__).resolve())
        env = os.environ | {"COHORT_ARCH": arch, "PATH": str(shim) + ":" + os.environ["PATH"]}
        command = ["make", "build", "DOCKER_BUILD=false", "OPTIONS=--build_tag_filters=-network_plugins",
                   "BAZEL_OPTIONS=" + " ".join(flags), "TARGETS=" + " ".join(requested(label) for label in sorted(expected))]
        row["command"] = command
        with (q.TMP / (arch + "-make.log")).open("wb") as output:
            child = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "2100", *command],
                                   stdout=output, stderr=subprocess.STDOUT, env=env)
        row.update(native_make_exit=child.returncode, execution_finished_at=q.now())
        if child.returncode:
            row["status"] = "INCOMPLETE" if child.returncode in (124, 137, -9, -15) else "FAIL"
        q.save(arch, row)
        for cmd in ("build", "cquery"):
            prefix = q.TMP / (arch + "-" + cmd)
            if prefix.with_suffix(".json").exists():
                row[cmd] = events(prefix)
                if cmd == "cquery":
                    row[cmd]["configured_count"] = len(row[cmd].pop("configured"))
                    row[cmd]["completed_count"] = len(row[cmd].pop("completed"))
                    row[cmd].pop("output_metadata")
                q.save(arch, row)
        build = row.get("build", {})
        configured = {(l, a, c) for l, a, _, _, c in build.get("configured", [])}
        passed = {(l, a, c) for l, a, c, ok in build.get("completed", []) if ok}
        failed = {(l, a, c) for l, a, c, ok in build.get("completed", []) if not ok}
        row["coverage"] = {"configured": len(configured), "passed": len(configured & passed),
                           "failed": len(configured & failed), "unaccounted": sorted(configured - passed - failed),
                           "requested_without_configured": sorted(set(expected) - {r[0] for r in configured})}
        q.save(arch, row)
        compact = q.TMP / (arch + "-build.compact")
        if compact.is_file():
            row["runners"], row["actions"] = actions(compact, arch, tests)
            q.save(arch, row)
        if child.returncode:
            return row
        for cmd in ("build", "cquery"):
            observed = row[cmd]
            assert observed["native_exit"] == observed["exit"] == 0
            assert observed["invocation"] == observed["requested_invocation"] and observed["bazel"] == "9.3.1"
            assert observed["test_results"] == 0 and not observed["failed_action_count"]
            if cmd == "build":
                assert not observed["aborted"]
            else:
                assert all(event["reason"] == "NO_BUILD" for event in observed["aborted"])
            assert not observed.get("incomplete_tail_bytes")
        assert Counter((l, a, k, tuple(t)) for l, a, k, t, _ in build["configured"]) == Counter(
            (l, f[0], f[1], tuple(f[2])) for l, f in expected.items())
        assert passed == configured and not failed and len(configured) == len(expected)
        assert all(build["configurations"][config] == {"amd64": "k8", "arm64": "aarch64"}[arch]
                   for _, _, config in configured)
        assert set(row["runners"]) <= {"remote", "remote cache hit"}, row["runners"]
        assert len(row["actions"]) == 1 and {a["label"] for a in row["actions"]} == tests
        binaries = row["test_binaries"] = {}
        for action in row["actions"]:
            assert action["worker_matches"] and not action["cache"] and action["runner"] == "remote"
            assert action["exit"] == 0 and not action["status_nonempty"] and action["mnemonic"] == "GoLink"
            assert action["executable"].endswith("/builder")
            output, = action["outputs"]
            path = Path(output["path"])
            assert re.fullmatch(r"bazel-out/[A-Za-z0-9_-]+/bin/pkg/sentry/[A-Za-z0-9_./+-]+", str(path))
            assert ".." not in path.parts and path.resolve().is_relative_to(Path("bazel-out").resolve())
            name = action["label"].split(":")[1]
            assert path.parts[-2:] == (name + "_", name)
            binary = q.elf(path)
            assert binary["machine"] == {"amd64": 62, "arm64": 183}[arch] and binary["load"] and binary["entry"]
            assert os.access(path, os.X_OK)
            assert binary["sha256"] == output["digest"]["hash"] and binary["bytes"] == int(output["digest"]["sizeBytes"])
            binaries[action["label"]] = binary
        header_paths = []
        for line in (q.TMP / (arch + "-cquery.out")).read_text().splitlines():
            match line.split():
                case [execpath, "InstrumentorVariables.inc"]:
                    if execpath.endswith("/llvm/include/llvm/Transforms/IPO/InstrumentorVariables.inc"):
                        header_paths.append(Path(execpath))
        header, = header_paths
        assert re.fullmatch(r"bazel-out/[A-Za-z0-9_-]+/bin/external/llvm\+\+llvm\+llvm-project/llvm/include/llvm/Transforms/IPO/InstrumentorVariables.inc", str(header))
        assert header.resolve().is_relative_to(Path("bazel-out").resolve())
        raw = header.read_bytes()
        assert 0 < len(raw) < 1024 * 1024
        assert b"constexpr char InstrumentorRuntimeHelper[]" in raw
        assert b"@LLVM_INSTRUMENTOR_RUNTIME_HELPER@" not in raw
        row["template_output"] = {"owner": GENERATOR, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        row["status"] = "PASS"
    except (AssertionError, KeyError, ValueError, OSError, subprocess.SubprocessError) as error:
        row["collection_error"] = {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
    finally:
        row["verified_at"] = q.now()
        q.save(arch, row)
    return row


def main() -> int:
    if Path(sys.argv[0]).name == "bazel":
        return wrapped()
    if sys.argv[1] == "original-headers":
        return prior_headers()
    assert not any(os.environ.get(name) for name in ("BUILDBUDDY_API_KEY", "ACCESS", "GH_TOKEN", "GITHUB_TOKEN"))
    mode = sys.argv[1]
    if mode == "verdict":
        outcomes = {arch: json.loads(path.read_text()) for arch in q.PIN if (path := q.OUT / (arch + ".json")).exists()}
        failed = sorted(arch for arch, row in outcomes.items() if row["status"] != "PASS")
        missing = sorted(set(q.PIN) - set(outcomes))
        clean = q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
        prior = json.loads(path.read_text()) if (path := q.OUT / "original-headers.json").exists() else {}
        good = clean and not failed and not missing and prior.get("status") == "PASS"
        q.save("verdict", {"source": q.PRODUCT, "carrier": os.environ["GITHUB_SHA"], "verified": good,
                           "clean_source": clean, "failed": failed, "missing": missing,
                           "original_selected_owners": 436, "original_mapped_owners": 433, "selected_owners": 2,
                           "rebuilt_test_binaries_per_arch": 1, "original_header_count": sum(len(r["headers"]) for r in prior.get("architectures", {}).values()),
                           "original_headers_status": prior.get("status", "MISSING"), "selection_sha256": CENSUS_SHA,
                           "execution_finished_at": max((r.get("execution_finished_at", "") for r in outcomes.values()), default=None),
                           "verified_at": q.now(), "prior_whole_graph": 13355668355, "prior_paired_release": 13391208856, "prior_sentry_cohort": 13394940473,
                           "scope": "Paired remote Make build/cquery; no tests, Nogo verdicts, or guest execution"})
        return int(not good)
    assert mode in q.PIN and q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
    return int(phase(mode, selection())["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
