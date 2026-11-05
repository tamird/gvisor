#!/usr/bin/env python3
"""Rebuild the six failed host-layout owners on both declared remote workers."""

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
import uuid
from urllib.parse import urlsplit
import zipfile

import qualify as q

BASE = "f35d725669ae45e7617cf66e7ceee2f0f6307490"
ORIGINAL_SHA = "06e1c0d39dc6b8d0790c3ae10594fd0efb166c0c9e5ba74e8548e0e570ceaafd"
ORACLE_SHA = "f157600d487449c674d51cd73fd6f2dcb409bb407cef13af7010548a0cccef53"
PACKAGES = ("//pkg/abi/gasket", "//pkg/abi/ib", "//pkg/abi/linux")
ELF_OWNERS = {pkg + ":" + pkg.rsplit("/", 1)[1] + "_hostlayout_test" for pkg in PACKAGES}
GENERATORS = {owner + "_gen" for owner in ELF_OWNERS}
CENSUS_SHA = hashlib.sha256(("\n".join(sorted(ELF_OWNERS | GENERATORS)) + "\n").encode()).hexdigest()
SELECT = r"""
if .spawn then
  .spawn | . as $s |
  {kind:"action",owner:((.targetLabel//"")|sub("^@@?//";"//")),runner,mnemonic,cache:(.cacheHit//false),exit:(.exitCode//0),status_nonempty:((.status//"")!=""),
   platform:[.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]} +
  (if ((.mnemonic|IN("GoLink","GoHostLayout","TestRunner")) and ($tests|has(($s.targetLabel//"")|sub("^@@?//";"//")))) or (.exitCode//0)!=0 or (.status//"")!="" then
    {label:.targetLabel,mnemonic,executable:.args[0],test_executable:(if .mnemonic=="TestRunner" then .args[1] else null end),generator_args:(if .mnemonic=="GoHostLayout" then .args[1:7] else null end),digest,outputs,missing_bash:((.status//"")|test("/bin/bash.{0,100}(no such file|not found)";"i"))}
   else {} end)
elif .file and (.file.path|test("/bin/pkg/(abi|sync|state|seccomp)/")) then
  {kind:"file",id,path:.file.path,digest:.file.digest}
else empty end
"""


def normalized(label: str) -> str:
    return "//" + label.split("//", 1)[1] if label.startswith(("@@//", "@//")) else label


def selection() -> list:
    raw = (q.TMP / "cohort.zip").read_bytes()
    assert len(raw) == 20715 and hashlib.sha256(raw).hexdigest() == ORIGINAL_SHA
    rows = [[label, "", "_generate_hostlayout rule" if label in GENERATORS else "go_test rule", []]
             for label in sorted(GENERATORS | ELF_OWNERS)]
    old = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for arch in q.PIN:
            arm = json.loads(archive.read(arch + ".json"))
            failed = {label for label, _, _, ok in arm["build"]["completed"] if not ok}
            assert arm["source"] == BASE and arm["coverage"]["passed"] == 82 and failed == GENERATORS | ELF_OWNERS
            actions = [a for a in arm["actions"] if a["exit"] or a["status_nonempty"]]
            assert {a["label"] for a in actions} == GENERATORS and len(actions) == 3
            assert all(a["mnemonic"] == "Genrule" and a["executable"] == "/bin/bash" and a["exit"] == 1
                       and not a["cache"] and a["worker_matches"] and a["runner"] == "remote" for a in actions)
            old[arch] = {"failed": sorted(failed), "passed": 82, "invocation": arm["build"]["invocation"]}
    raw = (q.TMP / "hostlayout-oracle.zip").read_bytes()
    assert len(raw) == 8890 and hashlib.sha256(raw).hexdigest() == ORACLE_SHA
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        oracle = json.loads(archive.read("result.json"))
    assert oracle["status"] == "PASS" and oracle["public_generated_equal"]
    phase = next(p for p in oracle["phases"] if p["name"] == "candidate-generated")
    outputs = {label: phase["outputs"][label] for label in GENERATORS}
    phase = next(p for p in oracle["phases"] if p["name"] == "public-tests")
    cases = {owner: sorted(c["name"] for c in phase["cases"] if c["owner"] == owner and c["result"] == "pass")
             for owner in ELF_OWNERS}
    assert sorted(len(names) for names in cases.values()) == [4, 6, 204]
    q.save("selection", {"source": q.PRODUCT, "base": BASE, "previous_run": 42062279696,
                         "previous_artifact": 13432169291, "previous_sha256": ORIGINAL_SHA, "previous": old,
                         "current_records": rows, "labels_sha256": CENSUS_SHA, "oracle_artifact": 13291492843,
                         "oracle_sha256": ORACLE_SHA, "generator_oracle": outputs, "case_oracle": cases})
    return rows


def wrapped() -> int:
    """Reuse the whole-graph Make observer, retaining the actual Bazel exits."""
    arch = os.environ["COHORT_ARCH"]
    command, *args = sys.argv[1:]
    assert command in ("build", "cquery"), command
    assert "--build_tag_filters=-network_plugins" in args
    if command == "build":
        expected = json.loads((q.OUT / "selection.json").read_text())["current_records"]
        assert sorted(arg for arg in args if arg.startswith("//")) == sorted(row[0] for row in expected)
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


def missing_shell(text: str) -> list:
    # Project fixed error classes only. A Bash argv alone does not prove ENOENT.
    return [line_no for line_no, line in enumerate(text.splitlines(), 1)
            if re.search(r"/bin/bash.{0,100}(?:no such file|not found|ENOENT)", line, re.I)][:8]


def events(prefix: Path) -> dict:
    row = json.loads(prefix.with_suffix(".json").read_text())
    configured, completed, aborted, failures, configurations = [], [], [], [], {}
    file_sets, outputs, links = {}, [], []
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
                                    "reason": event["aborted"].get("reason"),
                                    "incompatible": event["aborted"].get("reason") == "SKIPPED" and
                                    "incompatib" in event["aborted"].get("description", "").lower()})
                action = event.get("action", {})
                if action.get("type") in ("GoLink", "GoHostLayout") and action.get("success"):
                    owner = key.get("actionCompleted", {})
                    label = normalized(owner.get("label", ""))
                    if any(label.startswith((package + ":", package + "/")) for package in PACKAGES):
                        links.append([label, owner.get("configuration", {}).get("id"), owner.get("primaryOutput")])
                if action and not action.get("success"):
                    owner = key.get("actionCompleted", {})
                    failure = {"owner": normalized(owner.get("label", "")), "mnemonic": action.get("type"),
                               "configuration": owner.get("configuration", {}).get("id"),
                               "primary_output": owner.get("primaryOutput"), "exit": action.get("exitCode"),
                               "failure_codes": {k: v.get("code") for k, v in action.get("failureDetail", {}).items()
                                                 if isinstance(v, dict)}}
                    message = action.get("failureDetail", {}).get("message", "")
                    if missing_shell(message):
                        failure["missing_bin_bash"] = {"source": "BEP_action_failure_detail",
                            "message_sha256": hashlib.sha256(message.encode()).hexdigest()}
                    for channel in ("stdout", "stderr"):
                        uri = urlsplit(action.get(channel, {}).get("uri", ""))
                        digest = re.fullmatch(r"/?(?:[A-Za-z0-9_.+-]+/)*blobs/([0-9a-f]{64})/([0-9]+)", uri.path)
                        if uri.scheme == "bytestream" and uri.netloc == "remote.buildbuddy.io" and digest:
                            failure[channel] = {"sha256": digest[1], "bytes": int(digest[2]), "scope": "CAS_IDENTITY_ONLY"}
                        elif uri.scheme == "file":
                            failure[channel] = {"scope": "LOCAL_FILE_NOT_READ"}
                    failures.append(failure)
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
               output_metadata=outputs, successful_selected_producers=sorted(links), test_results=attempts, diagnostics=q.diagnostics(prefix.with_suffix(".err")))
    console = prefix.with_suffix(".err")
    if console.is_file():
        raw = console.read_bytes()
        row["console_missing_bin_bash_lines"] = missing_shell(raw.decode(errors="replace"))
        row["console_sha256"] = hashlib.sha256(raw).hexdigest()
    return row


def actions(path: Path, arch: str, tests: set, owners: set) -> tuple:
    expected = {"Arch": arch, "OSFamily": "linux", "workload-isolation-type": "oci", "recycle-runner": "false",
                "container-image": "docker://gcr.io/distroless/static-debian13@" + q.PIN[arch]}
    counts, files, selected, placements = Counter(), {}, [], Counter()
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
                runner = item["runner"] or "unknown"
                counts[runner] += 1
                owner = item.pop("owner")
                if owner in owners:
                    placements[(owner, item["mnemonic"], runner, bool(item["cache"]),
                                int(item["exit"]), props == expected)] += 1
                if item["mnemonic"] == "TestRunner":
                    counts["TestRunner"] += 1
                if props != expected:
                    counts["different_platform"] += 1
                if "label" in item:
                    item["label"] = normalized(item["label"])
                    item["worker_matches"] = props == expected
                    if item["exit"] or item["status_nonempty"]:
                        counts["failed_actions"] += 1
                    if (item["label"] in tests and item["mnemonic"] in ("GoLink", "GoHostLayout", "TestRunner")) or counts["failed_actions"] <= 32:
                        selected.append(item)
            assert rows.wait() == 0
        assert reader.wait() == 0
    for item in selected:
        item["missing_outputs"] = [o["invalidOutputPath"] for o in item.get("outputs") or [] if "invalidOutputPath" in o]
        item["outputs"] = [files[o["outputId"]] for o in (item.pop("outputs", None) or []) if o.get("outputId") in files]
    return dict(counts), selected, [[*key, count] for key, count in sorted(placements.items())]


def phase(arch: str, original: list) -> dict:
    expected = {row[0]: row[1:4] for row in original}
    tests = {label for label, fields in expected.items() if fields[1] in ("go_test rule", "go_binary rule")}
    assert tests == ELF_OWNERS and len(expected) == 6
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
             "--config=hostlayout_proof",
             "--lockfile_mode=off", "--color=no", "--curses=no"]
    try:
        row["disk_free_before"] = os.statvfs(q.TMP).f_bavail * os.statvfs(q.TMP).f_frsize
        assert row["disk_free_before"] >= 20 * 1024**3
        shim = q.TMP / (arch + "-bin")
        shim.mkdir()
        (shim / "bazel").symlink_to(Path(__file__).resolve())
        env = os.environ | {"COHORT_ARCH": arch, "PATH": str(shim) + ":" + os.environ["PATH"]}
        command = ["make", "build", "DOCKER_BUILD=false", "OPTIONS=--build_tag_filters=-network_plugins",
                   "BAZEL_OPTIONS=" + " ".join(flags), "TARGETS=" + " ".join(sorted(expected))]
        row["command"] = [part if not part.startswith("TARGETS=") else "TARGETS=<6 bound labels>" for part in command]
        with (q.TMP / (arch + "-make.log")).open("wb") as output:
            child = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "1200", *command],
                                   stdout=output, stderr=subprocess.STDOUT, env=env)
        row.update(native_make_exit=child.returncode, execution_finished_at=q.now(),
                   make_status="PASS" if child.returncode == 0 else
                   "INCOMPLETE" if child.returncode in (124, 137, -9, -15) else "FAIL")
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
        exit_code = build.get("native_exit", build.get("exit"))
        row["build_status"] = "PASS" if exit_code == 0 else "FAIL" if exit_code not in (None, 124, 137, -9, -15) else "INCOMPLETE"
        configured = {(l, a, c) for l, a, _, _, c in build.get("configured", [])}
        passed = {(l, a, c) for l, a, c, ok in build.get("completed", []) if ok}
        failed = {(l, a, c) for l, a, c, ok in build.get("completed", []) if not ok}
        row["coverage"] = {"configured": len(configured), "passed": len(configured & passed),
                           "failed": len(configured & failed), "unaccounted": sorted(configured - passed - failed),
                           "requested_without_configured": sorted(set(expected) - {r[0] for r in configured}),
                           "incompatible": [r for r in build.get("aborted", []) if r["incompatible"]]}
        observed = {(label, aspect, kind, tuple(tags)) for label, aspect, kind, tags, _ in build.get("configured", [])}
        wanted = {(label, aspect, kind, tuple(tags)) for label, (aspect, kind, tags) in expected.items()}
        row["selector_delta"] = {"expected_only": sorted(wanted - observed), "observed_only": sorted(observed - wanted)}
        q.save(arch, row)
        compact = q.TMP / (arch + "-build.compact")
        if compact.is_file():
            row["runners"], row["actions"], row["owner_spawn_counts"] = actions(compact, arch, set(expected), set(expected))
            q.save(arch, row)
        # Keep successful outputs independently of a different owner's failure.
        binaries, output_errors = {}, []
        row["byte_verified_binaries"] = binaries
        row["output_check_errors"] = output_errors
        links = [a for a in row.get("actions", []) if a["label"] in tests and a["mnemonic"] == "GoLink"]
        row["go_link_owners"] = sorted(a["label"] for a in links)
        primary_outputs = {(label, path) for label, config, path in build.get("successful_selected_producers", [])
                           if (label, "", config) in passed}
        canonical = set()
        generated = {}
        row["generated"] = generated
        oracle = json.loads((q.OUT / "selection.json").read_text())
        for action in row.get("actions", []):
            label = action["label"]
            if label not in GENERATORS or action["mnemonic"] != "GoHostLayout" or action["exit"] or action["status_nonempty"]:
                continue
            for output in action["outputs"]:
                path = Path(output["path"])
                if (label, str(path)) not in primary_outputs:
                    continue
                assert action["runner"] == "remote" and not action["cache"] and action["worker_matches"]
                assert action["executable"].endswith("/tools/go_hostlayout/hostlayoutgen_/hostlayoutgen")
                package, name = label.removeprefix("//").split(":")
                cpu = {"amd64": "k8", "arm64": "aarch64"}[arch]
                assert path == Path("bazel-out") / (cpu + "-fastbuild") / "bin" / package / (name.removesuffix("_gen") + "_generated.go")
                assert action["generator_args"] == ["-package", package.split("/")[-1], "-importpath",
                                                    "gvisor.dev/gvisor/" + package, "-out", str(path)]
                assert path.resolve(strict=True).is_relative_to(Path("bazel-out").resolve())
                content = path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                assert output["digest"]["hash"] == digest and int(output["digest"]["sizeBytes"]) == len(content)
                names = sorted(re.findall(r"(?m)^func (TestHostLayout_[A-Za-z0-9_]+)\(", content.decode()))
                generated[label] = {"path": str(path), "bytes": len(content), "sha256": digest, "cases": names,
                    "matches_original": {"bytes": len(content), "sha256": digest} == oracle["generator_oracle"][label],
                    "matches_original_cases": names == oracle["case_oracle"][label.removesuffix("_gen")]}
                q.save(arch, row)
        for action in links:
            if action["exit"] or action["status_nonempty"]:
                continue
            try:
                assert action["worker_matches"] and not action["cache"] and action["runner"] == "remote"
                assert action["executable"].endswith("/builder")
                output, = action["outputs"]
                path = Path(output["path"])
                assert re.fullmatch(r"bazel-out/[A-Za-z0-9_-]+/bin/pkg/(?:abi|sync|state|seccomp)/[A-Za-z0-9_./+-]+", str(path))
                assert ".." not in path.parts
                name = action["label"].split(":")[1]
                assert path.parts[-2:] == (name + "_", name)
                assert re.fullmatch(r"[0-9a-f]{64}", output["digest"]["hash"]) and int(output["digest"]["sizeBytes"]) > 64
                if (action["label"], str(path)) not in primary_outputs:
                    continue  # Same label may also link in a tool or split configuration.
                canonical.add(action["label"])
                if action["label"] not in ELF_OWNERS:
                    continue  # Its immutable action output is recorded, not downloaded or executed.
                assert path.resolve().is_relative_to(Path("bazel-out").resolve())
                binary = q.elf(path)
                assert binary["machine"] == {"amd64": 62, "arm64": 183}[arch] and binary["load"] and binary["entry"]
                assert os.access(path, os.X_OK)
                assert binary["sha256"] == output["digest"]["hash"] and binary["bytes"] == int(output["digest"]["sizeBytes"])
                binaries[action["label"]] = binary
            except (AssertionError, KeyError, ValueError, OSError) as error:
                output_errors.append({"owner": action["label"], "type": type(error).__name__,
                                      "line": traceback.extract_tb(error.__traceback__)[-1].lineno})
            q.save(arch, row)
        row["verified_top_level_go_link_owners"] = sorted(canonical)
        row["byte_unverified_go_link_owners"] = sorted({a["label"] for a in links} - set(binaries))
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
        assert passed == configured and not failed and len(configured) == 6
        assert all(build["configurations"][config] == {"amd64": "k8", "arm64": "aarch64"}[arch]
                   for _, _, config in configured)
        assert set(row["runners"]) <= {"remote", "remote cache hit"}, row["runners"]
        assert set(generated) == GENERATORS and all(v["matches_original"] and v["matches_original_cases"] for v in generated.values())
        assert canonical == tests
        assert all(a["exit"] == 0 and not a["status_nonempty"] for a in links)
        assert not output_errors and set(binaries) == ELF_OWNERS
        row["status"] = "PASS"
    except (AssertionError, KeyError, ValueError, OSError, subprocess.SubprocessError) as error:
        row["collection_error"] = {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
    finally:
        row["verified_at"] = q.now()
        q.save(arch, row)
    return row


def package_tests() -> int:
    """Execute the three generated package tests on the declared AMD64 worker."""
    import qualification_report as report
    import xml.etree.ElementTree as ET

    arch = "amd64"
    work = q.TMP / "hostlayout-native-tests"
    work.mkdir()
    invocation = str(uuid.uuid4())
    row = {"status": "INCOMPLETE", "source": q.PRODUCT, "supplier": "c2f5f79dce24877679cede14b85666b7ca65c6de",
           "arch": arch, "invocation": invocation, "cases": {}, "started": q.now()}
    q.save("amd64-package-tests", row)
    try:
        assert q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
        old = json.loads((q.OUT / "amd64.json").read_text())
        expected = {label.removesuffix("_gen"): entry["cases"] for label, entry in old["generated"].items()
                    if entry["matches_original"] and entry["matches_original_cases"]}
        assert set(expected) == ELF_OWNERS
        bazel = str(q.TMP / "bin" / "bazel")
        exe = "//test/rbe/release_prewarmer:amd64"
        flags = ["--config=x86_64", "--extra_execution_platforms=" + exe, "--host_platform=" + exe,
                 "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
                 "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
                 "--extra_toolchains=//test/rbe/protoc_check:static_python",
                 "--noincompatible_exclude_starlark_flags_from_exec_config", "--features=fully_static_link",
                 "--incompatible_use_native_posix_test_wrapper", "--nocache_test_results",
                 "--modify_execution_info=^TestRunner$=+no-remote-cache", "--test_env=GO_TEST_WRAP_TESTV=1",
                 "--remote_download_outputs=toplevel", "--remote_accept_cached", "--build_tag_filters=-network_plugins",
                 "--build_event_publish_all_actions", "--nobuild_event_json_file_path_conversion",
                 "--lockfile_mode=off", "--keep_going", "--jobs=32", "--color=no", "--curses=no",
                 "--invocation_id=" + invocation, "--build_event_json_file=" + str(work / "test.bep"),
                 "--execution_log_compact_file=" + str(work / "test.compact")]
        with (work / "test.err").open("wb") as out:
            run = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "900", bazel, *q.PROOF,
                                  "test", *flags, *sorted(ELF_OWNERS)], stdout=out, stderr=subprocess.STDOUT)
        row.update(native_exit=run.returncode, returned=q.now(), diagnostics=q.diagnostics(work / "test.err"))
        q.save("amd64-package-tests", row)
        owners, results, completed = set(), [], {}
        for line in (work / "test.bep").open():
            event = json.loads(line)
            key = event.get("id", {})
            if "started" in event:
                row["observed_invocation"] = event["started"]["uuid"]
            if "finished" in event:
                row["bazel_exit"] = event["finished"]["exitCode"].get("code", 0)
            if "testResult" in event:
                ident, result = key["testResult"], event["testResult"]
                results.append({"identity": ident, "status": result.get("status"),
                    "cached_local": bool(result.get("cachedLocally")),
                    "cached_remote": bool(result.get("executionInfo", {}).get("cachedRemotely"))})
            if "targetCompleted" in key and "completed" in event:
                ident = key["targetCompleted"]
                if not ident.get("aspect") and normalized(ident["label"]) in ELF_OWNERS:
                    completed[normalized(ident["label"])] = bool(event["completed"].get("success"))
                    owners.add((normalized(ident["label"]), ident["configuration"]["id"]))
        row.update(results=results, completed=completed)
        q.save("amd64-package-tests", row)
        runners, actions_seen, _ = actions(work / "test.compact", arch, ELF_OWNERS, ELF_OWNERS)
        attempts = [a for a in actions_seen if a["mnemonic"] == "TestRunner"]
        row.update(runners=runners, attempts=attempts)
        q.save("amd64-package-tests", row)
        execroot = Path(subprocess.check_output([bazel, *q.PROOF, "info", "execution_root"], text=True,
                                                stderr=subprocess.DEVNULL, timeout=90).strip())
        cap = report.capture(work / "test.bep", work / "sanitized", execroot, work / "reports", expected_kind="test")
        row["capture_errors"] = len(cap["errors"])
        for artifact in cap["reports"]:
            label = normalized(artifact["identity"]["label"])
            if label not in ELF_OWNERS or artifact["name"] != "test.xml":
                continue
            assert any(artifact["identity"] == r["identity"] for r in results)
            assert label not in row["cases"]
            local = work / artifact["path"]
            counts = report.xml_cases(local)
            names = sorted(c.get("name", "") for c in ET.parse(local).getroot().iter("testcase"))
            row["cases"][label] = {"sha256": artifact["sha256"], "bytes": artifact["bytes"],
                                  "passed": counts["passed"], "failed": counts["failed"], "skipped": counts["skipped"],
                                  "match_generated": names == expected[label], "names": names}
            q.save("amd64-package-tests", row)
        assert row["observed_invocation"] == invocation and run.returncode == row["bazel_exit"] == 0
        assert len(owners) == len(results) == len(attempts) == 3 and set(completed) == ELF_OWNERS and all(completed.values())
        assert {(normalized(r["identity"]["label"]), r["identity"]["configuration"]["id"]) for r in results} == owners
        assert not any(runners.get(k) for k in ("different_platform", "failed_actions"))
        assert all(r["status"] == "PASSED" and not r["cached_local"] and not r["cached_remote"] for r in results)
        assert {a["label"] for a in attempts} == ELF_OWNERS and all(a["runner"] == "remote" and a["worker_matches"]
                   and not a["cache"] and not a["exit"] and not a["status_nonempty"] and Path(a["executable"]).name == "posix_tw"
                   and Path(a["test_executable"]).name == a["label"].split(":")[1] for a in attempts)
        assert not cap["errors"] and set(row["cases"]) == ELF_OWNERS
        assert all(v["match_generated"] and v["passed"] == len(expected[k]) and not v["failed"] and not v["skipped"]
                   for k, v in row["cases"].items())
        row["status"] = "PASS"
    except (AssertionError, KeyError, ValueError, OSError, subprocess.SubprocessError, ET.ParseError) as error:
        row["error"] = {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
    finally:
        row["verified"] = q.now()
        q.save("amd64-package-tests", row)
    return int(row["status"] != "PASS")


def main() -> int:
    if Path(sys.argv[0]).name == "bazel":
        return wrapped()
    assert not any(os.environ.get(name) for name in ("BUILDBUDDY_API_KEY", "ACCESS", "GH_TOKEN", "GITHUB_TOKEN"))
    mode = sys.argv[1]
    if mode == "packages":
        return package_tests()
    if mode == "verdict":
        outcomes = {arch: json.loads(path.read_text()) for arch in q.PIN if (path := q.OUT / (arch + ".json")).exists()}
        failed = sorted(arch for arch, row in outcomes.items() if row["status"] != "PASS")
        missing = sorted(set(q.PIN) - set(outcomes))
        clean = q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
        good = clean and not failed and not missing
        q.save("verdict", {"source": q.PRODUCT, "carrier": os.environ["GITHUB_SHA"], "verified": good,
                           "clean_source": clean, "failed": failed, "missing": missing,
                           "original_slice": 88, "previously_passed_owners": 82, "selected_owners": 6,
                           "expected_test_binaries_per_arch": 3, "expected_generators_per_arch": 3,
                           "expected_byte_verified_binaries_per_arch": 3,
                           "byte_verified_binaries": {arch: len(row.get("byte_verified_binaries", {})) for arch, row in outcomes.items()},
                           "selection_sha256": CENSUS_SHA,
                           "execution_finished_at": max((r.get("execution_finished_at", "") for r in outcomes.values()), default=None),
                           "verified_at": q.now(), "prior_whole_graph": 13355668355, "prior_paired_release": 13391208856, "prior_failed_slice": 13432169291,
                           "scope": "Paired remote Make build/cquery only. AMD64 package tests and the independent boot guest fixture have separate receipts."})
        return int(not good)
    assert mode in q.PIN and q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
    return int(phase(mode, selection())["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
