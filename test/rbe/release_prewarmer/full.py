#!/usr/bin/env python3
"""Build the two failed runsc roots after the reviewed secfuzz repair."""

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

ORIGINAL_SHA = "0118c5f6f0df380d17386d88e9726b7a5303b0be2214a9dfa1061ddb91820fdf"
CENSUS_SHA = "0cb53b3e57dc7cda223c594d34b5a53569b6e4ca2ab4e79f609c74bb9280aa54"
LABELS_SHA = "3bc1ef8cdc12775cd336951812f9a060a76cf99ff5738d967f22e209936d1e9d"
BUILD_CENSUS_SHA = "b212017f932919e0669136ad5560f4b096b95065ae47a0a64ec1e56306c334ba"
SIDECARS = {
    "//runsc/fdparking:fdparking_binary": "runsc-fd-parking",
    "//runsc/prewarmer:prewarmer_binary": "gvisor-sentry-prewarmer",
    "//runsc/procumounter:procumounter_binary": "gvisor-proc-umounter",
}
C_LINKS = {label.split(":")[0] + ":_linked/" + output: label for label, output in SIDECARS.items()}
ARCHES = ("amd64",)
FOCUSED = {"//runsc/boot/filter:filter_fuzz_test", "//runsc/boot/filter:filter_fuzz_test_nogo"}
GENERATOR = "//test/secfuzz:secfuzz_covermeup"
ELF_OWNERS = {"//runsc/boot/filter:filter_fuzz_test"}
PARTIAL_SHA = "b54ea7d46b310eefa15059b35d5815fde2bcc03e47c400c7d3c554b51c629027"


def partial() -> dict:
    raw = (q.TMP / "partial.zip").read_bytes()
    assert len(raw) == 32242 and hashlib.sha256(raw).hexdigest() == PARTIAL_SHA
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert len(archive.infolist()) == 5
        member = archive.getinfo("amd64.json")
        assert member.file_size == 239771
        row = json.loads(archive.read(member))
    assert row["source"] == "c43bcb023757ee2a2d31e656ac495a27cb29e3be"
    assert row["build"]["invocation"] == "2b5266eb-4270-459a-a70e-4c2ad7c4d340"
    assert row["build"]["native_exit"] == 1 and row["native_make_exit"] == 2
    assert row["coverage"]["configured"] == 231 and row["coverage"]["passed"] == 229
    assert row["coverage"]["failed"] == 2 and not row["coverage"]["unaccounted"]
    assert {r[0] for r in row["build"]["completed"] if not r[3]} == FOCUSED
    return row
SELECT = r"""
if .spawn then
  .spawn | . as $s |
  {kind:"action",runner,mnemonic,cache:(.cacheHit//false),exit:(.exitCode//0),status_nonempty:((.status//"")!=""),
   platform:[.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]} +
  (if (($tests|has($s.targetLabel|sub("^@@?//";"//"))) and (.mnemonic=="GoLink" or .mnemonic=="CppLink" or .mnemonic=="RunBinary")) or (.exitCode//0)!=0 or (.status//"")!="" then
    {label:.targetLabel,mnemonic,executable:.args[0],digest,outputs}
   else {} end)
elif .file and (.file.path|contains("/bin/runsc/") or contains("/bin/test/secfuzz/")) then
  {kind:"file",id,path:.file.path,digest:.file.digest}
else empty end
"""


def normalized(label: str) -> str:
    return "//" + label.split("//", 1)[1] if label.startswith(("@@//", "@//")) else label


def selection() -> list:
    raw = (q.TMP / "original.zip").read_bytes()
    assert len(raw) == 87540 and hashlib.sha256(raw).hexdigest() == ORIGINAL_SHA
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        member, = archive.infolist()
        assert member.filename == "verdict.json" and member.file_size == 3237403
        previous = json.loads(archive.read(member))
    assert previous["source"] == "f876b5047538c0bbe2de38ef3f306a2cab985aa7"
    phase = previous["phases"]["build"]
    assert phase["selected_coverage"] == {"passed": 2098, "failed": 1, "aborted": 0,
                                          "unaccounted": 5667, "contradictory": 0}
    build = phase["build"]
    done = {tuple(row[:3]) for row in build["completed"]}
    aborted = {(row["label"], row["aspect"], row["configuration"]) for row in build["aborted"]}
    missing = [row for row in build["configured"] if (row[0], row[1], row[4]) not in done | aborted]
    assert len(missing) == 5667
    rows = sorted(row for row in missing if row[0].startswith(("//runsc/", "//runsc:")))
    assert len(rows) == len({row[0] for row in rows}) == 237, "Original runsc owner count differs"
    assert hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest() == CENSUS_SHA
    assert hashlib.sha256(("\n".join(row[0] for row in rows) + "\n").encode()).hexdigest() == LABELS_SHA
    assert Counter(row[2] for row in rows) == {
        "go_library rule": 44, "nogo_test rule": 114, "go_test rule": 41,
        "go_binary rule": 13, "_frontend_executable rule": 6,
        "_run_generator rule": 3, "alias rule": 3, "_go_imports rule": 3,
        "go_stateify rule": 3, "genrule rule": 3, "cc_flags_supplier rule": 3, "sh_test rule": 1,
    }
    current = {row[0]: row[:4] for row in rows}
    mappings = []
    for package in ("runsc/boot/filter", "runsc/gofer/fs/filter", "runsc/gofer/net/filter"):
        label = "//" + package + ":filter_state_autogen"
        retired = label + "_with_imports"
        assert current[label][2:] == ["_go_imports rule", []]
        assert current.pop(retired)[2:] == ["go_stateify rule", []]
        current[label][2] = "go_stateify rule"
        mappings.append({"original": [label, retired], "current": label,
                         "reason": "native stateify replaces the goimports intermediate", "source": "tools/defs.bzl"})
    for label in SIDECARS:
        retired = label.split(":")[0] + ":no_pie_cc_flags"
        assert current.pop(retired)[2:] == ["cc_flags_supplier rule", []]
        assert current[label][2:] == ["genrule rule", []]
        current[label][2] = "freestanding_executable rule"
        mappings.append({"original": [label, retired], "current": label,
                         "reason": "declared sidecar transition and cc_binary replace the shell and flag supplier",
                         "source": "runsc/sidecar.bzl"})
    assert len(current) == 231
    partial()
    current_rows = sorted(row for label, row in current.items() if label in FOCUSED)
    assert len(current_rows) == 2
    paths = sorted({row[0].split(":")[0][2:] + "/BUILD" for row in rows})
    assert len(paths) == 61
    source_blobs = []
    for line in q.git("ls-tree", "-r", q.PRODUCT, "--", *paths).splitlines():
        identity, path = line.split("\t")
        mode, kind, digest = identity.split()
        assert mode == "100644" and kind == "blob" and re.fullmatch(r"[0-9a-f]{40}", digest)
        source_blobs.append([path, digest])
    assert [row[0] for row in source_blobs] == paths
    assert hashlib.sha256(json.dumps(source_blobs, separators=(",", ":")).encode()).hexdigest() == BUILD_CENSUS_SHA
    q.save("selection", {"original_run": 41886246923, "original_artifact": 13355668355,
                         "original_zip_sha256": ORIGINAL_SHA, "original_status": "INCOMPLETE",
                         "original_configured": 7766, "original_unaccounted": 5667,
                         "selected_original_records": rows, "selection_sha256": CENSUS_SHA,
                         "labels_sha256": LABELS_SHA, "current_records": current_rows, "reconciliations": mappings,
                         "previous_partial_artifact": 13402650978, "previous_partial_sha256": PARTIAL_SHA,
                         "prior_current_owner_count": 231, "prior_successes_retained": 229,
                         "current_owner_count": len(current_rows), "source_build_blobs": source_blobs,
                         "build_census_sha256": BUILD_CENSUS_SHA,
                         "expected_byte_verified_elf_owners": sorted(ELF_OWNERS),
                         "prior_same_production_release_artifact": 13391208856,
                         "release_overlap": ["//runsc:runsc", "//runsc/cmd/sentry:gvisor_sentry",
                             "//runsc/checkpointgofer:checkpointgofer_binary", "//runsc/cmd/metricserver:runsc-metric-server",
                             "//runsc/cmd/netgofer:runsc-netgofer", *SIDECARS],
                         "scope": "Focused AMD64 build only; one Go test executable and one Nogo wrapper are not test verdicts; no ARM64 cohort or guest execution"})
    return current_rows


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


def events(prefix: Path) -> dict:
    row = json.loads(prefix.with_suffix(".json").read_text())
    configured, completed, aborted, failures, configurations = [], [], [], [], {}
    file_sets, outputs, link_results = {}, [], []
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
                identity = key.get("actionCompleted", {})
                if action.get("type") in ("GoLink", "CppLink", "RunBinary") and (normalized(identity.get("label", "")).startswith(("//runsc/", "//runsc:")) or normalized(identity.get("label", "")) == GENERATOR):
                    link_results.append({"owner": normalized(identity["label"]), "configuration": identity.get("configuration", {}).get("id"),
                                         "output": identity.get("primaryOutput"), "success": bool(action.get("success")),
                                         "exit": action.get("exitCode", 0), "mnemonic": action["type"]})
                if (action := event.get("action")) is not None and not action.get("success"):
                    owner = key.get("actionCompleted", {})
                    failure = {"owner": normalized(owner.get("label", "")), "mnemonic": action.get("type"),
                               "configuration": owner.get("configuration", {}).get("id"),
                               "primary_output": owner.get("primaryOutput"), "exit": action.get("exitCode"),
                               "failure_codes": {k: v.get("code") for k, v in action.get("failureDetail", {}).items()
                                                 if isinstance(v, dict)}}
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
               output_metadata=outputs, link_results=link_results, test_results=attempts, diagnostics=q.diagnostics(prefix.with_suffix(".err")))
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
                    if (item["label"] in tests and item["mnemonic"] in ("GoLink", "CppLink", "RunBinary")) or counts["failed_actions"] <= 32:
                        selected.append(item)
            assert rows.wait() == 0
        assert reader.wait() == 0
    for item in selected:
        item["outputs"] = [files[o["outputId"]] for o in (item.pop("outputs", None) or []) if o.get("outputId") in files]
    return dict(counts), selected


def phase(arch: str, original: list) -> dict:
    expected = {row[0]: row[1:4] for row in original}
    tests = {label for label, fields in expected.items() if fields[1] in ("go_test rule", "go_binary rule")}
    frontends = {label for label, fields in expected.items() if fields[1] == "_frontend_executable rule"}
    tests |= {label + "_/" + label.split(":")[1] for label in frontends}
    c_links = {owner: label for owner, label in C_LINKS.items() if label in expected}
    assert len(tests) == 1 and not frontends and not c_links and ELF_OWNERS == tests
    row = {"source": q.PRODUCT, "arch": arch, "status": "INCOMPLETE", "started": q.now(),
           "requested_count": len(expected), "selection_sha256": CENSUS_SHA}
    q.save(arch, row)
    exe = "//test/rbe/release_prewarmer:" + arch
    flags = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
             "--extra_execution_platforms=" + exe, "--host_platform=" + exe,
             "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
             "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
             "--extra_toolchains=//test/rbe/protoc_check:static_python" + ("_arm64" if arch == "arm64" else ""),
             "--remote_accept_cached", "--remote_download_outputs=minimal", "--jobs=32",
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
        row["command"] = [part if not part.startswith("TARGETS=") else "TARGETS=<the two original failed AMD64 roots>" for part in command]
        with (q.TMP / (arch + "-make.log")).open("wb") as output:
            child = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "2100", *command],
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
        q.save(arch, row)
        compact = q.TMP / (arch + "-build.compact")
        if compact.is_file():
            row["runners"], row["actions"] = actions(compact, arch, tests | c_links.keys() | {GENERATOR})
            q.save(arch, row)
        # Keep successful outputs independently of a different owner's failure.
        binaries, output_errors = {}, []
        row["byte_verified_binaries"] = binaries
        row["output_check_errors"] = output_errors
        links = [a for a in row.get("actions", []) if a["label"] in tests | c_links.keys() and a["mnemonic"] in ("GoLink", "CppLink")]
        row["expected_go_link_owners"] = sorted(tests)
        row["expected_c_link_owners"] = sorted(c_links)
        row["selected_link_action_counts"] = dict(Counter("cache_hit" if a["cache"] else "executed" for a in links))
        row["go_link_owners"] = sorted({a["label"] for a in links if a["mnemonic"] == "GoLink"})
        row["c_link_owners"] = sorted({a["label"] for a in links if a["mnemonic"] == "CppLink"})
        for action in links:
            if action["exit"] or action["status_nonempty"]:
                continue
            try:
                assert action["worker_matches"] and action["runner"] in ("remote", "remote cache hit")
                if action["mnemonic"] == "GoLink":
                    assert action["executable"].endswith("/builder")
                else:
                    assert action["executable"] == "external/llvm++llvm_toolchain_minimal+llvm-toolchain-minimal-linux-" + arch + "/bin/clang++"
                output, = action["outputs"]
                path = Path(output["path"])
                assert re.fullmatch(r"bazel-out/[A-Za-z0-9_-]+/bin/runsc/[A-Za-z0-9_./+-]+", str(path))
                assert ".." not in path.parts
                evidence = [r for r in build["link_results"] if r["owner"] == action["label"] and r["output"] == str(path)]
                assert len(evidence) <= 1 and (evidence or action["cache"])
                assert not evidence or (evidence[0]["success"] and evidence[0]["exit"] == 0)
                action["configuration"] = evidence[0]["configuration"] if evidence else None
                action["cpu"] = build["configurations"].get(action["configuration"])
                action["configuration_binding"] = "BEP_NATIVE" if evidence else "UNKNOWN_REMOTE_CACHE"
                if evidence:
                    assert action["cpu"] == {"amd64": "k8", "arm64": "aarch64"}[arch]
                name = action["label"].split(":")[1]
                if action["label"] in C_LINKS:
                    assert path.name == SIDECARS[C_LINKS[action["label"]]]
                elif "_/" not in name:
                    assert path.parts[-2:] == (name + "_", name)
                else:
                    root = name.split("_/")[0]
                    out = root.replace("_race_coverage", "_race_cov").replace("_coverage", "_cov")
                    assert path.name == out
                assert re.fullmatch(r"[0-9a-f]{64}", output["digest"]["hash"]) and int(output["digest"]["sizeBytes"]) > 64
                if action["label"] not in ELF_OWNERS:
                    continue  # Its immutable action output is recorded, not downloaded or executed.
                assert path.resolve().is_relative_to(Path("bazel-out").resolve())
                binary = q.elf(path)
                assert binary["machine"] == {"amd64": 62, "arm64": 183}[arch] and binary["load"] and binary["entry"]
                assert os.access(path, os.X_OK)
                assert binary["sha256"] == output["digest"]["hash"] and binary["bytes"] == int(output["digest"]["sizeBytes"])
                if action["label"] in C_LINKS:
                    assert not binary["interp"] and not binary["dynamic"]
                    if C_LINKS[action["label"]] != "//runsc/fdparking:fdparking_binary":
                        assert binary["bytes"] <= 4096
                # The same label may have target and execution configurations.
                binary.update(owner=action["label"], configuration=action["configuration"], path=str(path),
                              action_digest=action["digest"], cached=action["cache"], runner=action["runner"])
                binaries[str(path)] = binary
            except (AssertionError, KeyError, ValueError, OSError) as error:
                output_errors.append({"owner": action["label"], "type": type(error).__name__,
                                      "line": traceback.extract_tb(error.__traceback__)[-1].lineno})
            q.save(arch, row)
        row["byte_unverified_go_link_owners"] = sorted({a["label"] for a in links if a["mnemonic"] == "GoLink"} - {b["owner"] for b in binaries.values()})
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
        assert set(row["go_link_owners"]) == tests and set(row["c_link_owners"]) == set(c_links)
        assert len({(a["label"], a["digest"]["hash"]) for a in links}) == len(links)
        assert all(a["exit"] == 0 and not a["status_nonempty"] for a in links)
        assert not output_errors and {b["owner"] for b in binaries.values()} == ELF_OWNERS
        generators = [a for a in row["actions"] if a["label"] == GENERATOR]
        row["generator_actions"] = generators
        q.save(arch, row)
        assert len(generators) == 2
        cpus = set()
        for action in generators:
            assert action["mnemonic"] == "RunBinary" and action["runner"] == "remote" and not action["cache"]
            assert action["worker_matches"] and action["exit"] == 0 and not action["status_nonempty"]
            assert re.fullmatch(r"bazel-out/[A-Za-z0-9_-]+/bin/test/secfuzz/coveragegen/coveragegen_/coveragegen", action["executable"])
            output, = action["outputs"]
            assert re.fullmatch(r"bazel-out/[A-Za-z0-9_-]+/bin/test/secfuzz/secfuzz_covermeup.go", output["path"])
            match, = [r for r in build["link_results"] if r["owner"] == GENERATOR and r["output"] == output["path"]]
            assert match["success"] and match["exit"] == 0 and match["mnemonic"] == "RunBinary"
            action["configuration"] = match["configuration"]
            action["cpu"] = build["configurations"][match["configuration"]]
            cpus.add(action["cpu"])
        assert cpus == {"k8", "aarch64"}
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
    assert not any(os.environ.get(name) for name in ("BUILDBUDDY_API_KEY", "ACCESS", "GH_TOKEN", "GITHUB_TOKEN"))
    mode = sys.argv[1]
    if mode == "verdict":
        outcomes = {arch: json.loads(path.read_text()) for arch in ARCHES if (path := q.OUT / (arch + ".json")).exists()}
        failed = sorted(arch for arch, row in outcomes.items() if row["status"] != "PASS")
        missing = sorted(set(ARCHES) - set(outcomes))
        clean = q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
        good = clean and not failed and not missing
        q.save("verdict", {"source": q.PRODUCT, "carrier": os.environ["GITHUB_SHA"], "verified": good,
                           "clean_source": clean, "failed": failed, "missing": missing,
                           "original_selected_owners": 237, "prior_mapped_owners": 231, "selected_owners": 2, "prior_successful_owners_not_repeated": 229,
                           "expected_go_test_owners_per_arch": 1, "expected_go_link_owners_per_arch": 1, "expected_c_link_owners_per_arch": 0,
                           "expected_byte_verified_owners_per_arch": 1, "expected_nogo_wrappers_per_arch": 1,
                           "byte_verified_binaries": {arch: len(row.get("byte_verified_binaries", {})) for arch, row in outcomes.items()},
                           "selection_sha256": CENSUS_SHA,
                           "execution_finished_at": max((r.get("execution_finished_at", "") for r in outcomes.values()), default=None),
                           "verified_at": q.now(), "prior_whole_graph": 13355668355, "prior_paired_release": 13391208856, "prior_sentry_recovery": 13395599873,
                           "scope": "Focused AMD64 remote Make build/cquery; no tests, Nogo verdicts, ARM64 cohort retry, or guest execution"})
        return int(not good)
    assert mode in ARCHES and q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
    return int(phase(mode, selection())["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
