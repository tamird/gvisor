"""Verify both lean releases on the existing AMD64 remote execution platform."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import traceback
import uuid

import qualify as q

STAGE = "//:release"
LINKS = {
    "//runsc:runsc": "runsc",
    "//shim:containerd-shim-runsc-v1": "containerd-shim-runsc-v1",
    "//runsc/cmd/sentry:gvisor_sentry": "gvisor-bin/gvisor_sentry",
    "//runsc/checkpointgofer:checkpointgofer_binary": "gvisor-bin/checkpointgofer",
    "//runsc/cmd/netgofer:runsc-netgofer": "gvisor-bin/runsc-netgofer",
    "//runsc/cmd/metricserver:runsc-metric-server": "gvisor-bin/runsc-metric-server",
}
FILES = set(LINKS.values()) | {
    "gvisor-bin/gvisor-sentry-prewarmer", "gvisor-bin/runsc-fd-parking",
    "gvisor-bin/gvisor-proc-umounter",
}
EXEC = "amd64"
SDK = "@@rules_go++go_sdk+main___download_0_linux_amd64//:builder"
AUTH = "@@protobuf+//bazel/private/oss/toolchains/prebuilt:authenticity_validation"
LOADER = "//pkg/sentry/platform/kvm:mmapfilter_precompiled_gen"
C_LINKS = {
    "//runsc/prewarmer:_linked/gvisor-sentry-prewarmer": "gvisor-bin/gvisor-sentry-prewarmer",
    "//runsc/procumounter:_linked/gvisor-proc-umounter": "gvisor-bin/gvisor-proc-umounter",
    "//runsc/fdparking:_linked/runsc-fd-parking": "gvisor-bin/runsc-fd-parking",
}
PRODUCERS = {**LINKS, **C_LINKS}
STATEIFY = {"//pkg/unet:unet_state_autogen", "//pkg/marshal/primitive:primitive_state_autogen"}
STUB = "//pkg/sentry/platform/systrap/sysmsg:sighandler_data"
STUB_LINK = "//pkg/sentry/platform/systrap/sysmsg:sighandler.built-in.bin.o"
ROOTS = [STAGE, *sorted(STATEIFY), "//pkg/sentry/platform/systrap/sysmsg:sighandler_binary_arch",
         "//pkg/sentry/platform/systrap/sysmsg:sighandler_go_arch"]
PRIOR_SDK = {"artifact": 13457565460, "archive_sha256": "3ee75a8a200c326811b7e8c5563f17020bbf4799b340db30e7a1ecf028eb411b",
             "source": "f4717ffbfc7a0f3f138224bcaf6404835a24477f", "invocation": "c9979c1f-bbcc-4c3c-9da2-b82c29ef993c",
             "sha256": "ef6344cc4a011c3b4ca53705dbe0b1ebff965fc6038171e93466e985f32aa29b", "bytes": 7803259}


SELECT = r"""
if .spawn then
  .spawn | . as $s |
  {kind:"action",runner,cache:(.cacheHit//false),exit:(.exitCode//0),status:((.status//"")|if .=="" then "" else "reported" end),platform:
    [.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]} +
  (if (.targetLabel==$owner or .targetLabel==$stub or (.targetLabel==$stub_link and .mnemonic=="CppLink") or ($stateify|index($s.targetLabel))!=null or .mnemonic=="GoToolchainBinaryBuild" or .mnemonic=="ProtocAuthenticityCheck" or ((.mnemonic=="GoLink" or .mnemonic=="CppLink") and ($links|has($s.targetLabel))) or
       (.exitCode//0)!=0 or (.status//"")!="") then
    {label:.targetLabel,mnemonic,executable:.args[0],digest,outputs,
     argv:(if .targetLabel==$stub then .args else [] end),
     temp:[.envVars[]? | select(.name|IN("TMP","TMPDIR","TEMP","GOTMPDIR")) | .name]}
   else {} end)
elif .file and (.file.path|test("/bin/(release/|runsc/.*_/|runsc/[^/]+/_linked/|shim/.*_/)|/validation_output[.]txt$|/bin/external/rules_go[+][+]go_sdk[+]main___download_0_linux_amd64/builder[^/]*$|/pkg/(unet/unet|marshal/primitive/primitive)_state_autogen[.]go$|/sighandler[.](built-in[.]bin([.]o)?|go[.]arch)$")) then
  {kind:"file",id,path:.file.path,digest:.file.digest}
else empty end
"""


def configured_events(path):
    configs, links, roots = {}, {}, []
    for line in path.open():
        event = json.loads(line)
        key = event["id"]
        if "configuration" in event:
            configs[key["configuration"]["id"]] = event["configuration"].get("cpu")
        owner = key.get("targetCompleted", {})
        if owner.get("label") == STAGE and not owner.get("aspect"):
            roots.append(owner["configuration"]["id"])
        action = key.get("actionCompleted", {})
        if action.get("label") in {STAGE, *PRODUCERS, SDK, AUTH, LOADER, *STATEIFY, STUB, STUB_LINK} and "action" in event:
            body = event["action"]
            links[(action["label"], body["type"], action["primaryOutput"])] = (
                action["configuration"]["id"], bool(body.get("success")))
    return configs, links, roots


def make_bazel(arch, command, args):
    assert command in ("build", "cquery")
    assert not (q.TMP / ("make-" + arch + "-" + command + ".bep")).exists()
    assert "--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch] in args and "--host_platform=//test/rbe/release_prewarmer:amd64" in args
    assert "--extra_execution_platforms=//test/rbe/release_prewarmer:amd64" in args
    base = q.TMP / ("make-" + arch + "-" + command)
    invocation = str(uuid.uuid4())
    flags = ["--invocation_id=" + invocation, "--build_event_json_file=" + str(base) + ".bep"]
    if command == "build":
        flags += ["--execution_log_compact_file=" + str(base) + ".compact"]
        assert STAGE in args
    else:
        assert "--starlark:file=tools/show_paths.bzl" in args and any(STAGE in a for a in args)
    result = subprocess.run(["bazel", *q.PROOF, command, *flags, *args])
    Path(str(base) + ".json").write_text(json.dumps({"command": command, "invocation": invocation,
                                                     "exit": result.returncode}))
    return result.returncode


def actions(path, arch, owner=STAGE):
    expected = {"Arch": arch, "OSFamily": "linux", "workload-isolation-type": "oci",
                "recycle-runner": "false", "container-image": "docker://gcr.io/distroless/static-debian13@" + q.PIN[arch]}
    counts = Counter()
    files, selected = {}, []
    failed = 0
    with subprocess.Popen([str(q.TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as reader:
        with subprocess.Popen(["jq", "-c", "--arg", "owner", owner, "--argjson", "links",
                               json.dumps(PRODUCERS), "--argjson", "stateify", json.dumps(sorted(STATEIFY)), "--arg", "stub", STUB, "--arg", "stub_link", STUB_LINK, SELECT], stdin=reader.stdout, stdout=subprocess.PIPE,
                              text=True) as rows:
            reader.stdout.close()
            for line in rows.stdout:
                item = json.loads(line)
                if item["kind"] == "file":
                    files[item["id"]] = item
                    continue
                props = {p["name"]: p["value"] for p in item.pop("platform")}
                runner = item["runner"] or "unknown"
                counts[runner] += 1
                if props != expected:
                    counts["different_platform"] += 1
                if item.get("label"):
                    if item["label"] not in {owner, *PRODUCERS, SDK, AUTH, *STATEIFY, STUB, STUB_LINK}:
                        failed += 1
                        if failed > 12:
                            continue
                    item["worker_matches"] = props == expected
                    selected.append(item)
            assert rows.wait() == 0
        assert reader.wait() == 0
    for item in selected:
        item["outputs"] = [files[o["outputId"]] for o in (item.pop("outputs", None) or [])
                           if o.get("outputId") in files]
    return dict(counts), selected


def compiler_result(arch):
    bep, compact, log = [q.TMP / (arch + x) for x in (".bep", ".compact", ".log")]
    row = {"arch": arch, "source": q.PRODUCT, "exec_arch": EXEC, "target_arch": arch, "worker_pin": q.PIN[EXEC], "started": q.now(), "status": "INCOMPLETE",
           "requested_invocation": str(uuid.uuid4())}
    q.save(arch, row)
    exe = "//test/rbe/release_prewarmer:" + EXEC
    config = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch], "-c", "opt",
              "--extra_execution_platforms=" + exe, "--host_platform=" + exe,
              "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
              "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
              "--keep_going", "--remote_download_regex=(.*/validation_output[.]txt|.*/mmapfilter_precompiled[.]go[.]gen[.]go|.*/(unet|primitive)_state_autogen[.]go|.*/sighandler[.](built-in[.]bin([.]o)?|go[.]arch))$",
              "--remote_accept_cached", "--remote_download_outputs=toplevel", "--jobs=32",
              "--lockfile_mode=off", "--color=no", "--curses=no"]
    try:
        with log.open("w") as output:
            run = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "1800",
                                  "bazel", *q.PROOF, "build", *config,
                                  "--invocation_id=" + row["requested_invocation"], "--build_event_publish_all_actions",
                                  "--build_event_json_file=" + str(bep), "--execution_log_compact_file=" + str(compact),
                                  *ROOTS], stdout=output, stderr=subprocess.STDOUT)
        row.update(native_exit=run.returncode, returned=q.now())
        if run.returncode:
            row["status"] = "INCOMPLETE" if run.returncode in (124, 137, -9, -15) else "FAIL"
        q.save(arch, row)
        row["events"] = q.events(bep)
        row["diagnostics"] = q.diagnostics(log) if run.returncode else []
        if run.returncode and bep.is_file():
            failures = []
            for line in bep.open():
                event = json.loads(line)
                action = event.get("action", {})
                if not action or action.get("success", False) or len(failures) >= 12:
                    continue
                key = event["id"].get("actionCompleted", {})
                detail = action.get("failureDetail", {})
                failures.append({"owner": key.get("label"), "output": key.get("primaryOutput"),
                                 "type": action.get("type"), "exit": action.get("exitCode"),
                                 "reason": {k: v.get("code") for k, v in detail.items() if isinstance(v, dict)},
                                 "stderr": q.stream_identity(action.get("stderr", {})),
                                 "failure_tokens": q.failure_tokens(detail.get("message", "").encode())})
            row["failed_actions"] = failures
        q.save(arch, row)
        if compact.is_file():
            row["runners"], row["actions"] = actions(compact, EXEC)
        q.save(arch, row)
        # These controls are collected even if a later release action failed.
        controls(row, bep)
        q.save(arch, row)
        if run.returncode:
            return row
        assert row["events"]["invocation"] == row["requested_invocation"]
        assert row["events"]["test_results"] == 0
        assert row["events"]["owners"] == [True] and row["events"]["exit"] == 0 and row["events"]["bazel"] == "9.3.1"
        configs, observed, roots = configured_events(bep)
        root_config, = roots
        row["target_configuration"] = {"id": root_config, "cpu": configs[root_config]}
        assert configs[root_config] == {"amd64": "k8", "arm64": "aarch64"}[arch]
        root = Path("bazel-bin/release")
        targets = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
        assert targets == FILES
        row["binaries"] = binaries = {name: q.elf(root / name) for name in sorted(FILES)}
        q.save(arch, row)
        assert all(b["machine"] == {"amd64": 62, "arm64": 183}[arch] and not b["interp"] and not b["dynamic"] and b["load"] and b["entry"]
                   and os.access(root / name, os.X_OK) for name, b in binaries.items())
        prewarmer = binaries["gvisor-bin/gvisor-sentry-prewarmer"]
        assert 0 < prewarmer["bytes"] <= 4096 and not prewarmer["interp"] and not prewarmer["dynamic"]
        matched, linked = set(), set()
        for a in row["actions"]:
            if a["label"] not in {STAGE, *PRODUCERS}:
                continue
            assert a["worker_matches"] and not a["status"] and not a["exit"]
            assert a["runner"] == "remote" and not a["cache"]
            if a["label"] == STAGE:
                assert a["runner"] == "remote" and not a["cache"]
                assert a["mnemonic"] == "CopyFile" and a["executable"] == "external/bazel_lib++toolchains+coreutils_linux_amd64/coreutils"
                o, = a["outputs"]
                name = o["path"].split("/bin/release/")[1]
                assert name not in matched
                matched.add(name)
            else:
                name = PRODUCERS[a["label"]]
                if a["label"] in LINKS:
                    assert a["mnemonic"] == "GoLink" and a["executable"].endswith(
                        "/external/rules_go++go_sdk+main___download_0_linux_amd64/builder_reset/builder")
                else:
                    assert a["mnemonic"] == "CppLink" and a["executable"].endswith(("/bin/clang++", "/link-wrapper-clang++"))
                assert name not in linked
                linked.add(name)
                matching = [o for o in a["outputs"] if o["digest"]["hash"] == binaries[name]["sha256"]]
                o, = matching
            cfg, success = observed[(a["label"], a["mnemonic"], o["path"])]
            a["configuration"] = {"id": cfg, "cpu": configs[cfg]}
            assert configs[cfg] == {"amd64": "k8", "arm64": "aarch64"}[arch] and success
            assert o["digest"]["hash"] == binaries[name]["sha256"] and int(o["digest"]["sizeBytes"]) == binaries[name]["bytes"]
        assert matched == FILES and linked == FILES
        assert not row["runners"].get("different_platform")
        assert set(row["runners"]) <= {"remote", "remote cache hit"}, row["runners"]
        installed = q.TMP / (arch + "-installation")
        installed.mkdir()
        with log.open("a") as output:
            copied = subprocess.run(["make", "copy", "DOCKER_BUILD=false",
                                     "BAZEL=" + sys.executable + " " + str(Path(__file__).resolve()) + " make-bazel " + arch,
                                     "BAZEL_OPTIONS=" + " ".join(config),
                                     "TARGETS=" + STAGE, "DESTINATION=" + str(installed)],
                                    stdout=output, stderr=subprocess.STDOUT, timeout=600)
        row.update(make_exit=copied.returncode, execution_finished_at=q.now())
        if copied.returncode:
            row.update(status="FAIL", diagnostics=q.diagnostics(log))
            return row
        row["make_commands"] = []
        for command in ("build", "cquery"):
            base = q.TMP / ("make-" + arch + "-" + command)
            call = json.loads(Path(str(base) + ".json").read_text())
            events = q.events(Path(str(base) + ".bep"))
            call.update(events=events)
            row["make_commands"].append(call)
            assert events["invocation"] == call["invocation"] and events["exit"] == call["exit"] == 0
            assert events["test_results"] == 0
            if command == "build":
                assert events["owners"] == [True]
                call["runners"], _ = actions(Path(str(base) + ".compact"), EXEC)
                assert set(call["runners"]) <= {"remote", "remote cache hit"}
        installed_files = {str(p.relative_to(installed)) for p in installed.rglob("*") if p.is_file()}
        row["installed_files"] = sorted(installed_files)
        assert installed_files == FILES
        for name in installed_files:
            mode = (installed / name).lstat().st_mode
            assert stat.S_ISREG(mode) and mode & 0o111 == 0o111
            assert q.elf(installed / name) == binaries[name]
        assert row["bootstrap"]["status"] == "PASS"
        assert row["generator_outputs"]["status"] == "PASS"
        assert row["stateify"]["status"] == "PASS"
        assert row["stub_outputs"]["status"] == "PASS"
        row["status"] = "PASS"
    except (AssertionError, KeyError, ValueError, OSError, subprocess.SubprocessError) as err:
        row.update(collection_error=type(err).__name__, line=traceback.extract_tb(err.__traceback__)[-1].lineno)
    finally:
        row.setdefault("execution_finished_at", row.get("returned"))
        row["verified_at"] = q.now()
        q.save(arch, row)
    return row


def controls(row, bep):
    configs, observed, _ = configured_events(bep)
    row["bootstrap"] = bootstrap = {"status": "INCOMPLETE", "owner": SDK, "actions": []}
    row["generator_outputs"] = outputs = {"status": "INCOMPLETE", "members": {}}
    try:
        selected = [a for a in row.get("actions", []) if a["label"] == SDK and a["mnemonic"] == "GoToolchainBinaryBuild"]
        if not selected and row["arch"] == "arm64":
            prior = json.loads((q.OUT / "amd64.json").read_text())
            assert prior["source"] == row["source"] and prior["exec_arch"] == EXEC
            assert prior["worker_pin"] == row["worker_pin"] and prior["bootstrap"]["status"] == "PASS"
            assert prior["bootstrap"]["actions"] and all(a["runner"] in ("remote", "remote cache hit") and a["exit"] == 0 for a in prior["bootstrap"]["actions"])
            bootstrap.update(status="PASS", basis="SAME_SOURCE_PRIOR_INVOCATION", invocation=prior["events"]["invocation"],
                             reused_actions=prior["bootstrap"]["actions"])
        else:
            assert selected
        for action in selected:
            item = {"digest": action["digest"], "executable": action["executable"],
                    "runner": action["runner"], "cache": action["cache"], "exit": action["exit"],
                    "outputs": action["outputs"]}
            bootstrap["actions"].append(item)
            assert action["runner"] in ("remote", "remote cache hit") and action["worker_matches"]
            assert action["exit"] == 0 and not action["status"]
            assert action["executable"].endswith("/test/rbe/go_bootstrap/ash")
            binary, = [o for o in action["outputs"] if o["path"].endswith("/builder")]
            evidence = observed.get((SDK, "GoToolchainBinaryBuild", binary["path"]))
            if evidence:
                cfg, success = evidence
                item["configuration"] = {"id": cfg, "cpu": configs.get(cfg)}
                assert success and configs[cfg] in ("amd64", "k8")
            else:
                assert action["cache"] or action["runner"] == "remote cache hit"
                item["configuration"] = {"id": None, "cpu": None, "basis": "PRIOR_IDENTICAL_SDK_OUTPUT"}
            assert int(binary["digest"]["sizeBytes"]) > 0 and len(binary["digest"]["hash"]) == 64
            if action["cache"] or action["runner"] == "remote cache hit":
                assert binary["digest"]["hash"] == PRIOR_SDK["sha256"] and int(binary["digest"]["sizeBytes"]) == PRIOR_SDK["bytes"]
                item["previous_output"] = PRIOR_SDK
        if selected:
            fresh = sum(a["runner"] == "remote" and not a["cache"] for a in selected)
            bootstrap.update(status="PASS", fresh=fresh, cached=len(selected)-fresh, basis="CURRENT_ACTION_RECORDS")
    except (AssertionError, KeyError, ValueError, OSError) as error:
        bootstrap["collection_error"] = type(error).__name__
        bootstrap["line"] = traceback.extract_tb(error.__traceback__)[-1].lineno
    for label, mnemonic in ((AUTH, "ProtocAuthenticityCheck"), (LOADER, "TemplateExpand")):
        member = outputs["members"][label] = {"status": "INCOMPLETE", "outputs": []}
        try:
            # Hidden validation groups may be omitted from TargetComplete.
            # Successful action identities select files across actual configs.
            matches = [(k, v) for k, v in observed.items() if k[:2] == (label, mnemonic)]
            assert matches
            for key, (cfg, success) in matches:
                item = {"output": key[2], "configuration": {"id": cfg, "cpu": configs.get(cfg)}, "success": success}
                member["outputs"].append(item)
                assert success
                assert configs[cfg] in ("k8", "amd64", "aarch64")
                path = Path(key[2])
                assert path.resolve().is_relative_to(Path("bazel-out").resolve()) and path.is_file()
                raw = path.read_bytes()
                item.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                if label == AUTH:
                    assert raw == b"libprotoc 35.1\n"
                    action, = [a for a in row["actions"] if a["label"] == AUTH and any(o["path"] == key[2] for o in a["outputs"])]
                    output, = [o for o in action["outputs"] if o["path"] == key[2]]
                    assert output["digest"]["hash"] == item["sha256"] and int(output["digest"]["sizeBytes"]) == len(raw)
                    item.update(runner=action["runner"], cache=action["cache"])
                    assert not action["exit"] and not action["status"] and action["worker_matches"]
                    assert action["runner"] in ("remote", "remote cache hit")
                else:
                    expected = Path("pkg/seccomp/precompiledseccomp/load_programs.go.in").read_text()
                    expected = expected.replace("{RULES_IMPORT}", 'rules "gvisor.dev/gvisor/pkg/sentry/platform/kvm/mmapfilter"')
                    expected = expected.replace("{LOAD_PROGRAMS}", "rules.PrecompiledPrograms")
                    assert raw == expected.encode()
                    item["scope"] = "Bazel template output; no remote execution claim"
            expected_cpus = ("k8", "amd64") if label == AUTH else ({"amd64": "k8", "arm64": "aarch64"}[row["arch"]],)
            member["expected_cpus"] = list(expected_cpus)
            assert any(item["configuration"]["cpu"] in expected_cpus for item in member["outputs"])
            member["status"] = "PASS"
        except (AssertionError, KeyError, ValueError, OSError) as error:
            member["collection_error"] = type(error).__name__
            member["line"] = traceback.extract_tb(error.__traceback__)[-1].lineno
    if all(m["status"] == "PASS" for m in outputs["members"].values()):
        outputs["status"] = "PASS"

    row["stateify"] = stateify = {"status": "INCOMPLETE", "members": {}, "original_artifact": 13457565460}
    for owner in sorted(STATEIFY):
        member = stateify["members"][owner] = {"status": "INCOMPLETE", "actions": []}
        try:
            selected = [a for a in row.get("actions", []) if a["label"] == owner and a["mnemonic"] == "GoStateify"]
            assert selected
            for action in selected:
                output, = action["outputs"]
                item = {"digest": action["digest"], "output": output, "runner": action["runner"], "cache": action["cache"],
                        "executable": action["executable"], "exit": action["exit"]}
                member["actions"].append(item)
                assert action["runner"] == "remote" and not action["cache"] and action["worker_matches"]
                assert action["exit"] == 0 and not action["status"]
                assert action["executable"].endswith("/tools/go_stateify/stateify_/stateify")
                cfg, success = observed[(owner, "GoStateify", output["path"])]
                item["configuration"] = {"id": cfg, "cpu": configs.get(cfg)}
                assert success and configs[cfg] in ("k8", "amd64", "aarch64")
                path = Path(output["path"])
                assert path.resolve().is_relative_to(Path("bazel-out").resolve()) and path.is_file()
                raw = path.read_bytes()
                item["bytes"] = len(raw)
                item["sha256"] = hashlib.sha256(raw).hexdigest()
                assert raw and output["digest"]["hash"] == item["sha256"] and int(output["digest"]["sizeBytes"]) == len(raw)
            member["status"] = "PASS"
        except (AssertionError, KeyError, ValueError, OSError) as error:
            member["collection_error"] = type(error).__name__
            member["line"] = traceback.extract_tb(error.__traceback__)[-1].lineno
    if all(m["status"] == "PASS" for m in stateify["members"].values()):
        stateify["status"] = "PASS"

    row["stub_outputs"] = stub = {"status": "INCOMPLETE", "actions": [], "original_failure_artifact": 13456983300,
                                   "retired_owner": "//pkg/sentry/platform/systrap/sysmsg:sighandler_binary"}
    try:
        selected = [a for a in row.get("actions", []) if a["label"] == STUB and a["mnemonic"] == "RunBinary"]
        assert selected
        machines = set()
        for action in selected:
            item = {"digest": action["digest"], "runner": action["runner"], "cache": action["cache"], "exit": action["exit"], "files": {}}
            stub["actions"].append(item)
            assert action["runner"] == "remote" and not action["cache"] and action["worker_matches"]
            assert action["exit"] == 0 and not action["status"]
            executable, elf_path, blob_path, go_path = action["argv"]
            assert executable == action["executable"] and executable.endswith("/sysmsg/gen_stub_/gen_stub")
            assert {o["path"] for o in action["outputs"]} == {blob_path, go_path}
            link, = [a for a in row["actions"] if a["label"] == STUB_LINK and any(o["path"] == elf_path for o in a["outputs"])]
            assert link["mnemonic"] == "CppLink" and link["worker_matches"] and link["exit"] == 0 and not link["status"]
            assert link["runner"] == "remote" and not link["cache"]
            cfg, success = observed[(STUB_LINK, "CppLink", elf_path)]
            assert success and configs[cfg] in ("k8", "aarch64")
            item["configuration"] = {"id": cfg, "cpu": configs[cfg]}
            for path, witness in ((elf_path, link), (blob_path, action), (go_path, action)):
                p = Path(path)
                assert p.resolve().is_relative_to(Path("bazel-out").resolve()) and p.is_file() and p.stat().st_size < 10000000
                raw = p.read_bytes()
                output, = [o for o in witness["outputs"] if o["path"] == path]
                meta = {"path": path, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                assert output["digest"]["hash"] == meta["sha256"] and int(output["digest"]["sizeBytes"]) == len(raw)
                item["files"][Path(path).name] = meta
            # The RunBinary BEP identifies one primary output, not both outputs.
            assert any(observed.get((STUB, "RunBinary", path)) == (cfg, True) for path in (blob_path, go_path))
            elf, blob, exports = q.stub_elf(Path(elf_path).read_bytes())
            item["elf"] = elf
            assert elf["machine"] == {"k8": 62, "aarch64": 183}[configs[cfg]] and not elf["interpreter"]
            assert blob and Path(blob_path).read_bytes() == blob
            names = {m.group(1): int(m.group(2), 16) for m in re.finditer(r"var Sighandler_blob_offset__(\w+) = (0x[0-9a-fA-F]+)", Path(go_path).read_text())}
            item["exports"] = names
            assert names and names == exports
            item.update(blob_matches=True, offsets_match=True)
            machines.add(elf["machine"])
        assert machines == {62, 183}
        stub["status"] = "PASS"
    except (AssertionError, KeyError, ValueError, OSError, struct.error) as error:
        stub["collection_error"] = type(error).__name__
        stub["line"] = traceback.extract_tb(error.__traceback__)[-1].lineno
    if stub["status"] != "PASS" and row["arch"] == "arm64":
        # Both split arch inputs can already be complete from the AMD build.
        # Re-read those exact bytes; do not force an otherwise redundant action.
        try:
            prior = json.loads((q.OUT / "amd64.json").read_text())
            assert prior["source"] == row["source"] and prior["exec_arch"] == row["exec_arch"]
            assert prior["worker_pin"] == row["worker_pin"] and prior["stub_outputs"]["status"] == "PASS"
            verified = prior["stub_outputs"]["actions"]
            assert verified and {a["elf"]["machine"] for a in verified} == {62, 183}
            files = {}
            for action in verified:
                assert action["runner"] == "remote" and not action["cache"] and action["exit"] == 0
                assert action["blob_matches"] and action["offsets_match"]
                for meta in action["files"].values():
                    path = Path(meta["path"])
                    assert path.resolve().is_relative_to(Path("bazel-out").resolve()) and path.is_file()
                    raw = path.read_bytes()
                    assert len(raw) == meta["bytes"] and hashlib.sha256(raw).hexdigest() == meta["sha256"]
                    files[meta["path"]] = meta
            for action in row.get("actions", []):
                if action["label"] not in (STUB, STUB_LINK):
                    continue
                assert action["worker_matches"] and action["exit"] == 0 and not action["status"]
                assert action["runner"] in ("remote", "remote cache hit")
                for output in action["outputs"]:
                    meta = files[output["path"]]
                    assert output["digest"]["hash"] == meta["sha256"] and int(output["digest"]["sizeBytes"]) == meta["bytes"]
            completed = set()
            for line in bep.open():
                event = json.loads(line)
                target = event["id"].get("targetCompleted", {})
                if target.get("label") in ROOTS[3:] and not target.get("aspect"):
                    assert event.get("completed", {}).get("success")
                    completed.add(target["label"])
            assert completed == set(ROOTS[3:])
            stub["current_evidence_limitation"] = {"error": stub.pop("collection_error", None), "line": stub.pop("line", None)}
            stub.update(status="PASS", basis="SAME_SOURCE_PRIOR_INVOCATION", prior_invocation=prior["events"]["invocation"],
                        reused_actions=verified, current_completed_helpers=sorted(completed))
        except (AssertionError, KeyError, ValueError, OSError) as error:
            stub["reuse_error"] = type(error).__name__
            stub["reuse_line"] = traceback.extract_tb(error.__traceback__)[-1].lineno


def main():
    assert not any(os.environ.get(name) for name in ("BUILDBUDDY_API_KEY", "ACCESS", "GH_TOKEN", "GITHUB_TOKEN"))
    mode, *args = sys.argv[1:]
    if mode == "make-bazel":
        return make_bazel(args[0], args[1], args[2:])
    assert q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
    assert mode in ("amd64", "arm64") and not args
    return int(compiler_result(mode)["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
