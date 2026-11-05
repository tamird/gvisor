#!/usr/bin/env python3
"""Build the remaining syscall binaries and native launch scaffolds remotely."""

from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import traceback
import uuid
from urllib.parse import urlsplit
import zipfile

import qualify as q

ORIGINAL_SHA = "0118c5f6f0df380d17386d88e9726b7a5303b0be2214a9dfa1061ddb91820fdf"
CENSUS_SHA = "c2b207cc8466e5a3f55e2d92f969c88bc61d6fad0efa1b04e39933f709f8ce32"
LABELS_SHA = "79ee6a75e62c77a1ac4b387b109eb78cf7c865ed7af7f052ff121915ff03f94f"
BUILD_CENSUS_SHA = "56577fbea450a618d65efd16d0f4696deb1d63b0e61d4636d9576c60ccfb9453"
ARCHES = ("amd64", "arm64")
GROUPS = ("compiled", "scaffolds")
LAUNCHER = "//test/runner/setup_container:setup_container"
RSEQ = "//test/syscalls/linux/rseq:rseq"
LINK_FLAGS = {"-static", "-nostdlib", "-lpthread", "-ldl", "-lc", "-lstdc++", "-lc++", "-lc++abi", "-lunwind"}
COMPILED_ELFS = {RSEQ, "//test/syscalls/linux:proc_net_test", "//test/syscalls/linux:pidfd_test"}
SCAFFOLD_ELFS = {"//test/syscalls:proc_net_test_native", "//test/syscalls:proc_net_test_runsc_systrap_directfs"}
SOURCE_PATHS = ['test/syscalls/BUILD', 'test/syscalls/linux/BUILD', 'test/syscalls/linux/rseq/BUILD', 'test/runner/BUILD', 'test/runner/defs.bzl', 'test/runner/setup_container/BUILD', 'test/runner/setup_container/setup_container.cc']

SELECT = r"""
if .spawn then
  .spawn | . as $s |
  {kind:"action",runner,mnemonic,cache:(.cacheHit//false),exit:(.exitCode//0),status_nonempty:((.status//"")!=""),
   platform:[.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]} +
  (if (($tests|has($s.targetLabel|sub("^@@?//";"//"))) and (.mnemonic=="GoLink" or .mnemonic=="CppLink")) or (.exitCode//0)!=0 or (.status//"")!="" then
    {label:.targetLabel,mnemonic,executable:.args[0],digest,outputs} +
    (if ($s.targetLabel|sub("^@@?//";"//")) == "//test/syscalls/linux/rseq:rseq" then
       {rseq_flags:[.args[] | select(IN("-static","-nostdlib","-lpthread","-ldl","-lc","-lstdc++","-lc++","-lc++abi","-lunwind"))],
        library_flag_count:([.args[] | select(startswith("-l"))] | length),
        response_count:([.args[] | select(startswith("@"))] | length),
        response_paths:[.args[] | select(test("^@bazel-out/[A-Za-z0-9_-]+/bin/test/syscalls/linux/rseq/rseq[^/]*\\.params$")) | ltrimstr("@")]}
     else {} end)
   else {} end)
elif .file and ((.file.path|contains("/bin/test/syscalls/")) or (.file.path|contains("/bin/test/runner/setup_container/"))) then
  {kind:"file",id,path:.file.path,digest:.file.digest}
else empty end
"""


def normalized(label: str) -> str:
    return "//" + label.split("//", 1)[1] if label.startswith(("@@//", "@//")) else label


def prior_native() -> dict:
    row = {"artifact": 13385085477, "status": "INCOMPLETE", "scope": "Earlier native AMD64 build, not this product or RBE execution"}
    try:
        raw = (q.TMP / "prior-native.zip").read_bytes()
        assert len(raw) == 14386 and hashlib.sha256(raw).hexdigest() == "4a61f85bc02faaf11a8c14fcda7998d99ca2ad557055390472a8f0cf34d98727"
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            member, = archive.infolist()
            assert member.filename == "verdict.json" and member.file_size == 237299
            previous = json.loads(archive.read(member))
        assert previous["source"] == "9e6e8fa9c55583b3505646d32713e991231ceb47"
        native = previous["phases"]["native"]
        assert native["status"] == "PASS" and native["native_make_exit"] == 0
        labels = native["requested_labels"]
        assert len(labels) == len(set(labels)) == 166
        row.update(status="VERIFIED", source=previous["source"], labels=sorted(labels), zip_sha256=hashlib.sha256(raw).hexdigest())
    except (AssertionError, KeyError, ValueError, OSError) as error:
        row["error"] = {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
    return row


def prior_cohort() -> dict:
    row = {"artifact": 13408670826, "status": "INCOMPLETE", "source": "3e7f20cd67e5ef4b3c6715a167d5fbc9fab357aa"}
    try:
        raw = (q.TMP / "prior-cohort.zip").read_bytes()
        assert len(raw) == 173029 and hashlib.sha256(raw).hexdigest() == "370eacc2134cf49e094cb933820fd72c9982a114782cf59bb554bbf08b6323d6"
        arms = {}
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for arch in ARCHES:
                value = json.loads(archive.read(arch + "-compiled.json"))
                assert value["source"] == row["source"] and value["native_make_exit"] == 2
                assert value["build"]["native_exit"] == value["build"]["exit"] == 1
                assert value["coverage"]["configured"] == 301 and value["coverage"]["passed"] == 299 and value["coverage"]["failed"] == 2
                failed = sorted(r[0] for r in value["build"]["completed"] if not r[3])
                assert failed == sorted([RSEQ, "//test/syscalls/linux:rseq_test"])
                action, = value["build"]["failed_actions"]
                assert action["owner"] == RSEQ and action["mnemonic"] == "CppLink" and action["exit"] == 1
                assert action["stderr"] == {"sha256": "c12d20039f18ded628c9c94446645f63fb19a4e0595641fd777399a1d0f201e4", "bytes": 484, "scope": "CAS_IDENTITY_ONLY"}
                links = {}
                for action in value["actions"]:
                    if action["mnemonic"] != "CppLink" or action["exit"] or action["status_nonempty"]:
                        continue
                    assert action["worker_matches"] and action["runner"] == "remote" and not action["cache"]
                    output, = [o for o in action["outputs"] if o["path"].endswith("/bin/" + action["label"][2:].replace(":", "/"))]
                    links[action["label"]] = output["digest"]
                assert len(links) == 266
                arms[arch] = {"invocation": value["build"]["invocation"], "configured": 301, "passed": 299, "failed_labels": failed,
                              "failed_action": value["build"]["failed_actions"][0], "successful_link_outputs": links,
                              "test_results": value["build"]["test_results"], "scaffolds": "NOT_RUN"}
        row.update(status="VERIFIED", arches=arms, zip_sha256=hashlib.sha256(raw).hexdigest(),
                   diagnostic_receipt=13408878009, missing_libraries=["dl", "pthread"])
    except (AssertionError, KeyError, ValueError, OSError) as error:
        row["error"] = {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
    return row


def selection() -> list:
    raw = (q.TMP / "original.zip").read_bytes()
    assert len(raw) == 87540 and hashlib.sha256(raw).hexdigest() == ORIGINAL_SHA
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        member, = archive.infolist()
        assert member.filename == "verdict.json" and member.file_size == 3237403
        previous = json.loads(archive.read(member))
    assert previous["source"] == "f876b5047538c0bbe2de38ef3f306a2cab985aa7"
    phase = previous["phases"]["build"]
    assert phase["selected_coverage"] == {"passed": 2098, "failed": 1, "aborted": 0, "unaccounted": 5667, "contradictory": 0}
    build = phase["build"]
    done = {tuple(row[:3]) for row in build["completed"]}
    aborted = {(row["label"], row["aspect"], row["configuration"]) for row in build["aborted"]}
    missing = [row for row in build["configured"] if (row[0], row[1], row[4]) not in done | aborted]
    assert len(missing) == 5667
    rows = sorted(row for row in missing if row[0].startswith(("//test/syscalls/", "//test/syscalls:")))
    assert len(rows) == len({row[0] for row in rows}) == 3726
    assert hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest() == CENSUS_SHA
    assert hashlib.sha256(("\n".join(row[0] for row in rows) + "\n").encode()).hexdigest() == LABELS_SHA
    assert Counter(row[2] for row in rows) == {"_runner_test rule": 3424, "cc_binary rule": 266, "cc_library rule": 34, "cc_flags_supplier rule": 1, "genrule rule": 1}
    current = {row[0]: row[:4] for row in rows}
    retired = ["//test/syscalls/linux/rseq:no_pie_cc_flags", "//test/syscalls/linux/rseq:rseq_binary"]
    assert current.pop(retired[0])[2:] == ["cc_flags_supplier rule", []]
    assert current.pop(retired[1])[2:] == ["genrule rule", []]
    current[RSEQ] = [RSEQ, "", "cc_binary rule", []]
    for label, row in current.items():
        if row[2] == "_runner_test rule":
            assert label.startswith("//test/syscalls:") and "manual" not in row[3]
            row[2] = "native_test rule"
        else:
            assert label.startswith("//test/syscalls/linux")
    current_rows = sorted(current.values())
    groups = {name: [row[0] for row in current_rows if (row[2] == "native_test rule") == (name == "scaffolds")] for name in GROUPS}
    assert len(current_rows) == 3725 and {name: len(labels) for name, labels in groups.items()} == {"compiled": 301, "scaffolds": 3424}
    assert COMPILED_ELFS <= set(groups["compiled"]) and SCAFFOLD_ELFS <= set(groups["scaffolds"])
    source_blobs = []
    for line in q.git("ls-tree", "-r", q.PRODUCT, "--", *SOURCE_PATHS).splitlines():
        identity, path = line.split("\t")
        mode, kind, digest = identity.split()
        assert mode == "100644" and kind == "blob" and re.fullmatch(r"[0-9a-f]{40}", digest)
        source_blobs.append([path, digest])
    assert [row[0] for row in source_blobs] == sorted(SOURCE_PATHS)
    assert hashlib.sha256(json.dumps(source_blobs, separators=(",", ":")).encode()).hexdigest() == BUILD_CENSUS_SHA
    prior = prior_native()
    if prior["status"] == "VERIFIED":
        assert set(prior["labels"]) <= set(groups["compiled"])
    q.save("selection", {"source": q.PRODUCT, "original_run": 41886246923, "original_artifact": 13355668355,
        "original_zip_sha256": ORIGINAL_SHA, "original_status": "INCOMPLETE", "original_configured": 7766,
        "original_unaccounted": 5667, "selected_original_records": rows, "selection_sha256": CENSUS_SHA,
        "labels_sha256": LABELS_SHA, "current_records": current_rows, "groups": groups,
        "reconciliations": [{"original": retired, "current": [RSEQ], "source": "a0e3cf835fb57e9b08fb595a2854f0ddcb491f91", "reason": "Declared freestanding rseq binary replaces shell/flag supplier"},
            {"kind_before": "_runner_test rule", "kind_after": "native_test rule", "same_labels_and_tags": 3424, "source": "9771751fff91a8c1cfaa92268967e4a1bb9a8e0f", "reason": "Native executable symlink and existing setup_container replace generated Bash launcher"}],
        "current_owner_count": 3725, "source_build_blobs": source_blobs, "build_census_sha256": BUILD_CENSUS_SHA,
        "prior_native_coverage": prior, "prior_paired_control": prior_cohort(),
        "correction": {"source": q.PRODUCT, "path": "test/syscalls/linux/rseq/BUILD", "feature": "-static_link_cpp_runtimes", "scope": "Only the standalone freestanding binary"}, "test_execution_eligibility": "Not qualified: stock Bazel9.3.1 TestRunner/XML wrapper tools and per-fixture runtime facilities are outside this build-only result", "expected_compiled_elf_owners": sorted(COMPILED_ELFS),
        "expected_scaffold_elf_owners": sorted(SCAFFOLD_ELFS), "scaffold_runtime_tag_counts": dict(Counter(tag for row in current_rows if row[2] == "native_test rule" for tag in row[3])),
        "scope": "Paired build only; 267 C++ binaries, 34 libraries and 3424 native launch scaffolds. No syscall, TestRunner, Nogo or guest execution."})
    return current_rows

def wrapped() -> int:
    """Reuse the whole-graph Make observer, retaining the actual Bazel exits."""
    arch = os.environ["COHORT_ARCH"]
    group = os.environ["COHORT_GROUP"]
    assert arch in ARCHES and group in GROUPS
    command, *args = sys.argv[1:]
    assert command in ("build", "cquery"), command
    assert "--build_tag_filters=-network_plugins" in args
    if command == "build":
        expected = json.loads((q.OUT / "selection.json").read_text())["groups"][group]
        targets = ["//test/syscalls:all"] if group == "scaffolds" else expected
        assert sorted(arg for arg in args if arg.startswith("//")) == sorted(targets)
        downloads = [arg for arg in args if arg.startswith("--remote_download_outputs=")]
        assert downloads and downloads[-1] == "--remote_download_outputs=minimal"
    prefix = q.TMP / (arch + "-" + group + "-" + command)
    assert not prefix.with_suffix(".json").exists(), (arch, command)
    invocation = str(uuid.uuid4())
    row = {"command": command, "group": group, "selected_patterns": targets if command == "build" else None, "requested_invocation": invocation, "started": q.now()}
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
                if action.get("type") in ("GoLink", "CppLink") and normalized(identity.get("label", "")).startswith(("//test/syscalls/", "//test/syscalls:", "//test/runner/setup_container:")):
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
                    if (item["label"] in tests and item["mnemonic"] in ("GoLink", "CppLink")) or counts["failed_actions"] <= 32:
                        selected.append(item)
            assert rows.wait() == 0
        assert reader.wait() == 0
    for item in selected:
        if item["label"] == RSEQ and item["mnemonic"] == "CppLink":
            observation = {"status": "INCOMPLETE", "params": []}
            item["freestanding_arguments"] = observation
            try:
                flags = set(item.pop("rseq_flags"))
                library_count = item.pop("library_flag_count")
                paths = item.pop("response_paths")
                assert len(paths) == item.pop("response_count")
                for name in paths:
                    path = Path(name)
                    assert re.fullmatch(r"bazel-out/[A-Za-z0-9_-]+/bin/test/syscalls/linux/rseq/rseq[^/]*\.params", name)
                    assert path.resolve().is_relative_to(Path("bazel-out").resolve())
                    entry, = [f for f in files.values() if f["path"] == name]
                    assert path.is_file() and path.stat().st_size <= 65536
                    raw = path.read_bytes()
                    assert len(raw) == int(entry["digest"]["sizeBytes"]) and hashlib.sha256(raw).hexdigest() == entry["digest"]["hash"]
                    arguments = shlex.split(raw.decode())
                    flags.update(set(arguments) & LINK_FLAGS)
                    library_count += sum(arg.startswith("-l") for arg in arguments)
                    observation["params"].append({"sha256": entry["digest"]["hash"], "bytes": len(raw)})
                observation.update(status="VERIFIED", flags=sorted(flags),
                    explicit_library_flag_count=library_count,
                    explicit_freestanding_flags={"-static", "-nostdlib"} <= flags)
            except (AssertionError, KeyError, ValueError, OSError) as error:
                observation["error"] = {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
        item["outputs"] = [files[o["outputId"]] for o in (item.pop("outputs", None) or []) if o.get("outputId") in files]
    return dict(counts), selected


def phase(arch: str, group: str, original: list) -> dict:
    name = arch + "-" + group
    rows = [r for r in original if (r[2] == "native_test rule") == (group == "scaffolds")]
    expected = {r[0]: r[1:4] for r in rows}
    link_owners = {r[0] for r in rows if r[2] == "cc_binary rule"} if group == "compiled" else {LAUNCHER}
    wanted_elfs = COMPILED_ELFS if group == "compiled" else {LAUNCHER}
    row = {"source": q.PRODUCT, "arch": arch, "group": group, "status": "INCOMPLETE", "started": q.now(),
           "requested_count": len(expected), "selection_sha256": CENSUS_SHA, "scope": "BUILD_ONLY",
           "expected_link_owners": sorted(link_owners), "expected_byte_verified_owners": sorted(wanted_elfs),
           "expected_scaffold_witnesses": sorted(SCAFFOLD_ELFS) if group == "scaffolds" else []}
    q.save(name, row)
    exe = "//test/rbe/release_prewarmer:" + arch
    cpu, machine = {"amd64": ("k8", 62), "arm64": ("aarch64", 183)}[arch]
    flags = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
             "--extra_execution_platforms=" + exe, "--host_platform=" + exe,
             "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
             "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
             "--extra_toolchains=//test/rbe/protoc_check:static_python" + ("_arm64" if arch == "arm64" else ""),
             "--remote_accept_cached", "--jobs=32", "--lockfile_mode=off", "--color=no", "--curses=no"]
    if group == "compiled":
        flags.append("--materialize_param_files")
    try:
        if group == "scaffolds":
            prior_path = q.OUT / (arch + "-compiled.json")
            prerequisite = json.loads(prior_path.read_text()) if prior_path.exists() else {}
            row["compiled_build_status"] = prerequisite.get("build_status", "INCOMPLETE")
        row["disk_free_before"] = os.statvfs(q.TMP).f_bavail * os.statvfs(q.TMP).f_frsize
        assert row["disk_free_before"] >= 20 * 1024**3
        shim = q.TMP / (name + "-bin")
        shim.mkdir()
        (shim / "bazel").symlink_to(Path(__file__).resolve())
        env = os.environ | {"COHORT_ARCH": arch, "COHORT_GROUP": group, "PATH": str(shim) + ":" + os.environ["PATH"]}
        targets = "//test/syscalls:all" if group == "scaffolds" else " ".join(sorted(expected))
        command = ["make", "build", "DOCKER_BUILD=false",
                   "OPTIONS=--build_tag_filters=-network_plugins --remote_download_outputs=minimal",
                   "BAZEL_OPTIONS=" + " ".join(flags), "TARGETS=" + targets]
        row["command"] = [part if not part.startswith("TARGETS=") or group == "scaffolds" else "TARGETS=<301 exact compiled/library labels>" for part in command]
        row["timeout_seconds"] = 1800 if group == "compiled" else 1200
        with (q.TMP / (name + "-make.log")).open("wb") as output:
            child = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", str(row["timeout_seconds"]), *command],
                                   stdout=output, stderr=subprocess.STDOUT, env=env)
        row.update(native_make_exit=child.returncode, execution_finished_at=q.now(),
                   make_status="PASS" if child.returncode == 0 else
                   "INCOMPLETE" if child.returncode in (124, 137, -9, -15) else "FAIL")
        if child.returncode:
            row["status"] = row["make_status"]
        q.save(name, row)
        for cmd in ("build", "cquery"):
            prefix = q.TMP / (name + "-" + cmd)
            if prefix.with_suffix(".json").exists():
                row[cmd] = events(prefix)
                if cmd == "cquery":
                    row[cmd]["configured_count"] = len(row[cmd].pop("configured"))
                    row[cmd]["completed_count"] = len(row[cmd].pop("completed"))
                    row[cmd].pop("output_metadata")
                q.save(name, row)
        build = row.get("build", {})
        exit_code = build.get("native_exit", build.get("exit"))
        row["build_status"] = "PASS" if exit_code == 0 else "FAIL" if exit_code not in (None, 124, 137, -9, -15) else "INCOMPLETE"
        configured = {(l, a, c) for l, a, _, _, c in build.get("configured", [])}
        passed = {(l, a, c) for l, a, c, ok in build.get("completed", []) if ok}
        failed = {(l, a, c) for l, a, c, ok in build.get("completed", []) if not ok}
        actual_labels = {r[0] for r in configured}
        row["coverage"] = {"configured": len(configured), "passed": len(configured & passed),
            "failed": len(configured & failed), "unaccounted": sorted(configured - passed - failed),
            "requested_without_configured": sorted(set(expected) - actual_labels), "new_labels": sorted(actual_labels - set(expected)),
            "manual_labels": sorted(r[0] for r in build.get("configured", []) if "manual" in r[3]),
            "incompatible": [r for r in build.get("aborted", []) if r["incompatible"]],
            "default_outputs": dict(Counter(str(r[3]) for r in build.get("output_metadata", [])))}
        prior = json.loads((q.OUT / "selection.json").read_text())["prior_native_coverage"]
        if group == "compiled" and prior["status"] == "VERIFIED":
            prior_labels = set(prior["labels"])
            row["coverage"]["prior_native_amd64_owner_overlap"] = {
                "requested": len(set(expected) & prior_labels),
                "passed_here": len({l for l, _, _ in passed} & prior_labels),
                "passed_not_in_that_receipt": len({l for l, _, _ in passed} - prior_labels),
            }
        q.save(name, row)
        compact = q.TMP / (name + "-build.compact")
        if compact.is_file():
            row["runners"], row["actions"] = actions(compact, arch, link_owners)
            q.save(name, row)
        # A failed owner never hides another owner's successful output or exit.
        binaries, output_errors = {}, []
        row["byte_verified_binaries"] = binaries
        row["output_check_errors"] = output_errors
        links = [a for a in row.get("actions", []) if a["label"] in link_owners and a["mnemonic"] == "CppLink"]
        row["selected_link_action_counts"] = dict(Counter("cache_hit" if a["cache"] else "executed" for a in links))
        row["actual_link_owners"] = sorted({a["label"] for a in links})
        prior_control = json.loads((q.OUT / "selection.json").read_text())["prior_paired_control"]
        prior_outputs = prior_control.get("arches", {}).get(arch, {}).get("successful_link_outputs", {}) if group == "compiled" else {}
        row["prior_successful_output_matches"] = []
        for action in links:
            if action["exit"] or action["status_nonempty"]:
                continue
            try:
                assert action["worker_matches"] and action["runner"] in ("remote", "remote cache hit")
                assert action["executable"] in {
                    "external/llvm++llvm_toolchain_minimal+llvm-toolchain-minimal-linux-" + arch + "/bin/clang++",
                    "external/llvm+/tools/internal/link-wrapper-clang++/link-wrapper-clang++",
                }
                # CcInfo may also declare supplementary link outputs; select the
                # executable by its actual public owner rather than output order.
                executable = action["label"][2:].replace(":", "/")
                outputs = [o for o in action["outputs"] if o["path"].endswith("/bin/" + executable)]
                output, = outputs
                if action["label"] in prior_outputs and output["digest"] == prior_outputs[action["label"]]:
                    row["prior_successful_output_matches"].append({"owner": action["label"], "cache_hit": action["cache"], "digest": output["digest"]})
                path = Path(output["path"])
                assert re.fullmatch(r"bazel-out/[A-Za-z0-9_-]+/bin/test/[A-Za-z0-9_./+-]+", str(path)) and ".." not in path.parts
                evidence = [r for r in build["link_results"] if r["owner"] == action["label"] and r["output"] == str(path)]
                assert len(evidence) <= 1 and (evidence or action["cache"])
                assert not evidence or (evidence[0]["success"] and evidence[0]["exit"] == 0)
                action["configuration"] = evidence[0]["configuration"] if evidence else None
                action["cpu"] = build["configurations"].get(action["configuration"])
                action["configuration_binding"] = "BEP_NATIVE" if evidence else "UNKNOWN_REMOTE_CACHE"
                if evidence:
                    assert action["cpu"] == cpu
                assert re.fullmatch(r"[0-9a-f]{64}", output["digest"]["hash"]) and int(output["digest"]["sizeBytes"]) > 64
                if action["label"] not in wanted_elfs:
                    continue
                assert path.resolve().is_relative_to(Path("bazel-out").resolve()) and os.access(path, os.X_OK)
                binary = q.elf(path)
                assert binary["machine"] == machine and binary["load"] and binary["entry"]
                assert binary["sha256"] == output["digest"]["hash"] and binary["bytes"] == int(output["digest"]["sizeBytes"])
                if action["label"] == RSEQ:
                    assert not binary["interp"] and not binary["dynamic"]
                    assert not action["cache"] and action["runner"] == "remote"
                binary.update(owner=action["label"], configuration=action["configuration"], path=str(path),
                              action_digest=action["digest"], cached=action["cache"], runner=action["runner"])
                binaries[str(path)] = binary
            except (AssertionError, KeyError, ValueError, OSError) as error:
                output_errors.append({"owner": action["label"], "type": type(error).__name__,
                                      "line": traceback.extract_tb(error.__traceback__)[-1].lineno})
        row["byte_unverified_link_owners"] = sorted({a["label"] for a in links} - {b["owner"] for b in binaries.values()})
        row["scaffold_witnesses"] = []
        q.save(name, row)
        if group == "scaffolds":
            try:
                helpers = [b for b in binaries.values() if b["owner"] == LAUNCHER]
                helper, = helpers
                query_output = q.TMP / (name + "-cquery.out")
                pairs = [line.split() for line in query_output.read_text().splitlines() if line.strip()]
                wanted_names = {label.split(":")[1]: label for label in SCAFFOLD_ELFS}
                for fields in pairs:
                    assert len(fields) == 2
                    execpath, destination = fields
                    if destination not in wanted_names:
                        continue
                    path = Path(execpath)
                    assert re.fullmatch(r"bazel-out/[A-Za-z0-9_-]+/bin/test/syscalls/" + re.escape(destination), execpath)
                    assert path.resolve().is_relative_to(Path("bazel-out").resolve()) and os.access(path, os.X_OK)
                    binary = q.elf(path)
                    assert binary["machine"] == machine and (binary["sha256"], binary["bytes"]) == (helper["sha256"], helper["bytes"])
                    row["scaffold_witnesses"].append({"owner": wanted_names[destination], "path": execpath,
                        "sha256": binary["sha256"], "bytes": binary["bytes"], "machine": machine,
                        "helper_owner": LAUNCHER, "scope": "Built native executable scaffold; not executed"})
                assert {r["owner"] for r in row["scaffold_witnesses"]} == SCAFFOLD_ELFS and len(row["scaffold_witnesses"]) == 2
            except (AssertionError, KeyError, ValueError, OSError) as error:
                output_errors.append({"scope": "scaffold_witnesses", "type": type(error).__name__,
                                      "line": traceback.extract_tb(error.__traceback__)[-1].lineno})
        q.save(name, row)
        if child.returncode:
            return row
        for cmd in ("build", "cquery"):
            observed = row[cmd]
            assert observed["native_exit"] == observed["exit"] == 0
            assert observed["invocation"] == observed["requested_invocation"] and observed["bazel"] == "9.3.1"
            assert observed["test_results"] == 0 and not observed["failed_action_count"]
            assert not observed.get("incomplete_tail_bytes")
            if cmd == "build":
                assert not observed["aborted"]
            else:
                assert all(event["reason"] == "NO_BUILD" for event in observed["aborted"])
        assert Counter((l, a, k, tuple(t)) for l, a, k, t, _ in build["configured"]) == Counter(
            (l, f[0], f[1], tuple(f[2])) for l, f in expected.items())
        assert passed == configured and not failed and len(configured) == len(expected)
        assert all(build["configurations"][config] == cpu for _, _, config in configured)
        assert set(row["runners"]) <= {"remote", "remote cache hit"}, row["runners"]
        assert set(row["actual_link_owners"]) == link_owners
        assert len({(a["label"], a["digest"]["hash"]) for a in links}) == len(links)
        assert all(a["exit"] == 0 and not a["status_nonempty"] for a in links)
        if group == "compiled":
            rseq_action, = [a for a in links if a["label"] == RSEQ]
            observed = rseq_action["freestanding_arguments"]
            row["rseq_argument_observation"] = observed
            assert observed["status"] == "VERIFIED" and observed["explicit_library_flag_count"] == 0 and observed["explicit_freestanding_flags"]
        assert not output_errors and {b["owner"] for b in binaries.values()} == wanted_elfs
        if group == "scaffolds":
            assert all(r[3] is True for r in build["output_metadata"]) and len(build["output_metadata"]) == 3424
        row["status"] = "PASS"
    except (AssertionError, KeyError, ValueError, OSError, subprocess.SubprocessError) as error:
        row["collection_error"] = {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
    finally:
        row["verified_at"] = q.now()
        q.save(name, row)
    return row


def main() -> int:
    if Path(sys.argv[0]).name == "bazel":
        return wrapped()
    assert not any(os.environ.get(name) for name in ("BUILDBUDDY_API_KEY", "ACCESS", "GH_TOKEN", "GITHUB_TOKEN"))
    mode = sys.argv[1]
    if mode == "verdict":
        names = {arch + "-" + group for arch in ARCHES for group in GROUPS}
        outcomes = {name: json.loads(path.read_text()) for name in names if (path := q.OUT / (name + ".json")).exists()}
        failed = sorted(name for name, row in outcomes.items() if row["status"] != "PASS")
        missing = sorted(names - set(outcomes))
        clean = q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
        good = clean and not failed and not missing
        q.save("verdict", {"source": q.PRODUCT, "carrier": os.environ["GITHUB_SHA"], "verified": good,
            "clean_source": clean, "failed": failed, "missing": missing, "original_selected_owners": 3726,
            "selected_owners": 3725, "groups": {"compiled": 301, "scaffolds": 3424},
            "byte_verified_binaries": {name: len(row.get("byte_verified_binaries", {})) for name, row in outcomes.items()},
            "scaffold_witnesses": {name: len(row.get("scaffold_witnesses", [])) for name, row in outcomes.items()},
            "actual_target_coverage": {name: row.get("coverage", {}) for name, row in outcomes.items()},
            "actual_test_results": {name: row.get("build", {}).get("test_results") for name, row in outcomes.items()},
            "selection_sha256": CENSUS_SHA,
            "execution_finished_at": max((r.get("execution_finished_at", "") for r in outcomes.values()), default=None),
            "verified_at": q.now(), "prior_whole_graph": 13355668355, "prior_native_syscall_build": 13385085477, "prior_paired_syscall_build": 13408670826,
            "original_linker_diagnostic": 13408878009,
            "scope": "Paired remote build-only Make phases; no TestRunner, syscall, Nogo, or guest executions"})
        return int(not good)
    assert mode in ARCHES and sys.argv[2] in GROUPS
    assert q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
    return int(phase(mode, sys.argv[2], selection())["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
