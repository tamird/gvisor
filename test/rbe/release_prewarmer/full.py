"""Verify an ARM64 release built on the AMD64 remote execution platform."""

from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import traceback
import uuid
import zipfile

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
TARGET = "arm64"
C_LINKS = {
    "//runsc/prewarmer:_linked/gvisor-sentry-prewarmer": "gvisor-bin/gvisor-sentry-prewarmer",
    "//runsc/procumounter:_linked/gvisor-proc-umounter": "gvisor-bin/gvisor-proc-umounter",
    "//runsc/fdparking:_linked/runsc-fd-parking": "gvisor-bin/runsc-fd-parking",
}
PRODUCERS = {**LINKS, **C_LINKS}


SELECT = r"""
if .spawn then
  .spawn | . as $s |
  {kind:"action",runner,cache:(.cacheHit//false),exit:(.exitCode//0),status:((.status//"")|if .=="" then "" else "reported" end),platform:
    [.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]} +
  (if (.targetLabel==$owner or ((.mnemonic=="GoLink" or .mnemonic=="CppLink") and ($links|has($s.targetLabel))) or
       (.exitCode//0)!=0 or (.status//"")!="") then
    {label:.targetLabel,mnemonic,executable:.args[0],digest,outputs,
     temp:[.envVars[]? | select(.name|IN("TMP","TMPDIR","TEMP","GOTMPDIR")) | .name]}
   else {} end)
elif .file and (.file.path|test("/bin/(release/|runsc/.*_/|runsc/[^/]+/_linked/|shim/.*_/)")) then
  {kind:"file",id,path:.file.path,digest:.file.digest}
else empty end
"""


def native_control():
    raw = (q.TMP / "original.zip").read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    assert (len(raw), sha) == (6330, "58bdc86cb8651fe3abe87a7ac7f2b8127766f60c4fb5a80799966e9053610dcc")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert set(archive.namelist()) == {"amd64.json", "arm64.json", "original-actions.json", "verdict.json"}
        verdict = json.loads(archive.read("verdict.json"))
        assert verdict["source"] == q.PRODUCT and verdict["carrier"] == "719babac9f3d24482221c288439fa3740ca7b89f"
        assert verdict["verified"] and verdict["clean_source"] and verdict["executed"] == ["amd64", "arm64"]
        arms = {}
        for arch, machine in (("amd64", 62), ("arm64", 183)):
            row = json.loads(archive.read(arch + ".json"))
            assert row["status"] == "PASS" and row["source"] == q.PRODUCT and row["arch"] == arch
            assert row["native_exit"] == row["make_exit"] == row["events"]["test_results"] == 0
            assert set(row["installed_files"]) == set(row["binaries"]) == FILES
            assert all(b["machine"] == machine for b in row["binaries"].values())
            arms[arch] = {"invocation": row["events"]["invocation"], "runners": row["runners"],
                          "installed": len(FILES), "machine": machine, "status": row["status"]}
    q.save("native-control", {"run": 41975504552, "artifact": 13391208856, "sha256": sha,
                              "source": q.PRODUCT, "recorded_earlier": arms})


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
        if action.get("label") in {STAGE, *PRODUCERS} and "action" in event:
            body = event["action"]
            links[(action["label"], body["type"], action["primaryOutput"])] = (
                action["configuration"]["id"], bool(body.get("success")))
    return configs, links, roots


def make_bazel(command, args):
    assert command in ("build", "cquery")
    assert not (q.TMP / ("make-" + command + ".bep")).exists()
    assert "--config=aarch64" in args and "--host_platform=//test/rbe/release_prewarmer:amd64" in args
    assert "--extra_execution_platforms=//test/rbe/release_prewarmer:amd64" in args
    base = q.TMP / ("make-" + command)
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


def actions(path, arch):
    expected = {"Arch": arch, "OSFamily": "linux", "workload-isolation-type": "oci",
                "recycle-runner": "false", "container-image": "docker://gcr.io/distroless/static-debian13@" + q.PIN[arch]}
    counts = Counter()
    files, selected = {}, []
    failed = 0
    with subprocess.Popen([str(q.TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as reader:
        with subprocess.Popen(["jq", "-c", "--arg", "owner", STAGE, "--argjson", "links",
                               json.dumps(PRODUCERS), SELECT], stdin=reader.stdout, stdout=subprocess.PIPE,
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
                    if item["label"] not in {STAGE, *PRODUCERS}:
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


def compiler_result():
    arch = TARGET
    bep, compact, log = [q.TMP / (arch + x) for x in (".bep", ".compact", ".log")]
    row = {"arch": arch, "source": q.PRODUCT, "exec_arch": EXEC, "target_arch": TARGET, "worker_pin": q.PIN[EXEC], "started": q.now(), "status": "INCOMPLETE",
           "requested_invocation": str(uuid.uuid4())}
    q.save(arch, row)
    exe = "//test/rbe/release_prewarmer:" + EXEC
    config = ["--config=aarch64", "-c", "opt",
              "--extra_execution_platforms=" + exe, "--host_platform=" + exe,
              "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + EXEC + "_ash",
              "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
              "--extra_toolchains=//test/rbe/protoc_check:static_python",
              "--remote_accept_cached", "--remote_download_outputs=toplevel", "--jobs=32",
              "--lockfile_mode=off", "--color=no", "--curses=no"]
    try:
        with log.open("w") as output:
            run = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "4500",
                                  "bazel", *q.PROOF, "build", *config,
                                  "--invocation_id=" + row["requested_invocation"], "--build_event_publish_all_actions",
                                  "--build_event_json_file=" + str(bep), "--execution_log_compact_file=" + str(compact),
                                  STAGE], stdout=output, stderr=subprocess.STDOUT)
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
                                 "stderr": {k: action.get("stderr", {}).get(k) for k in ("digest", "length")}})
            row["failed_actions"] = failures
        q.save(arch, row)
        if compact.is_file():
            row["runners"], row["actions"] = actions(compact, EXEC)
        q.save(arch, row)
        if run.returncode:
            return row
        assert row["events"]["invocation"] == row["requested_invocation"]
        assert row["events"]["test_results"] == 0
        assert row["events"]["owners"] == [True] and row["events"]["exit"] == 0 and row["events"]["bazel"] == "9.3.1"
        configs, observed, roots = configured_events(bep)
        root_config, = roots
        row["target_configuration"] = {"id": root_config, "cpu": configs[root_config]}
        assert configs[root_config] == "aarch64"
        root = Path("bazel-bin/release")
        targets = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
        assert targets == FILES
        row["binaries"] = binaries = {name: q.elf(root / name) for name in sorted(FILES)}
        q.save(arch, row)
        assert all(b["machine"] == 183 and not b["interp"] and not b["dynamic"] and b["load"] and b["entry"]
                   and os.access(root / name, os.X_OK) for name, b in binaries.items())
        prewarmer = binaries["gvisor-bin/gvisor-sentry-prewarmer"]
        assert 0 < prewarmer["bytes"] <= 4096 and not prewarmer["interp"] and not prewarmer["dynamic"]
        matched, linked = set(), set()
        for a in row["actions"]:
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
            assert configs[cfg] == "aarch64" and success
            assert o["digest"]["hash"] == binaries[name]["sha256"] and int(o["digest"]["sizeBytes"]) == binaries[name]["bytes"]
        assert matched == FILES and linked == FILES
        assert not row["runners"].get("different_platform")
        assert set(row["runners"]) <= {"remote", "remote cache hit"}, row["runners"]
        installed = q.TMP / (arch + "-installation")
        installed.mkdir()
        with log.open("a") as output:
            copied = subprocess.run(["make", "copy", "DOCKER_BUILD=false",
                                     "BAZEL=" + sys.executable + " " + str(Path(__file__).resolve()) + " make-bazel",
                                     "BAZEL_OPTIONS=" + " ".join(config),
                                     "TARGETS=" + STAGE, "DESTINATION=" + str(installed)],
                                    stdout=output, stderr=subprocess.STDOUT, timeout=600)
        row.update(make_exit=copied.returncode, execution_finished_at=q.now())
        if copied.returncode:
            row.update(status="FAIL", diagnostics=q.diagnostics(log))
            return row
        row["make_commands"] = []
        for command in ("build", "cquery"):
            base = q.TMP / ("make-" + command)
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
        row["status"] = "PASS"
    except (AssertionError, KeyError, ValueError, OSError, subprocess.SubprocessError) as err:
        row.update(collection_error=type(err).__name__, line=traceback.extract_tb(err.__traceback__)[-1].lineno)
    finally:
        row.setdefault("execution_finished_at", row.get("returned"))
        row["verified_at"] = q.now()
        q.save(arch, row)
    return row


def main():
    assert not any(os.environ.get(name) for name in ("BUILDBUDDY_API_KEY", "ACCESS", "GH_TOKEN", "GITHUB_TOKEN"))
    mode, *args = sys.argv[1:]
    if mode == "make-bazel":
        return make_bazel(args[0], args[1:])
    assert q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
    if mode == "original":
        native_control()
        return 0
    assert mode == TARGET and not args
    return int(compiler_result()["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
