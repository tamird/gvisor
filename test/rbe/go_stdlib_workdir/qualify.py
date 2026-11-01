"""Build one runsc dependency and observe its actual Go actions on empty Linux."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native_packages"))

TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "go-temp-evidence"
BASE = "851d04fc2918adcaa60a78fd9fa7e766bc4fec68"
PACKAGE = "//pkg/gohacks:gohacks"
METADATA = "//test/rbe/go_stdlib_workdir:metadata"
PROBES = ["//test/rbe/go_stdlib_workdir:" + name for name in ("leave", "inspect")]
TARGETS = [PACKAGE, METADATA, *PROBES]
IMAGE = "docker://gcr.io/distroless/static-debian13@sha256:b0aca6ddfad947e66782a29534647936d4d645a1b96d9c8a97e4f53b1607d428"
PLATFORM = "//test/rbe/go_stdlib_workdir:worker"
FLAGS = ["--config=aarch64", "--host_platform=" + PLATFORM, "--extra_execution_platforms=" + PLATFORM,
         "--extra_toolchains=//test/rbe/go_bootstrap:linux_arm64_ash",
         "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain", "--remote_accept_cached",
         "--remote_download_outputs=toplevel", "--noremote_local_fallback", "--jobs=32",
         "--keep_going", "--color=no", "--curses=no"]
OUTPUT_NAMES = {"runtime.a", "stdlib.pkg.json", "gohacks.a", "gohacks.x", "leave.txt", "inspect.txt"}
EXPECTED = {"GoStdlib": "runtime.a", "GoStdlibList": "stdlib.pkg.json", "GoCompilePkg": "gohacks.a"}
FILTER = r'''
if .spawn != null and (.spawn.mnemonic | IN("GoStdlib","GoStdlibList","GoCompilePkg","ProbeWorkerTmp")) then
  .spawn | {kind:"spawn", label:.targetLabel, mnemonic, runner, cache:(.cacheHit//false), status,
    exit:(.exitCode//0), argv0:.args[0], digest,
    properties:[.platform.properties[]? | select(.name | IN("Arch","OSFamily","container-image","workload-isolation-type","recycle-runner"))],
    env:[.envVars[]? | select(.name | IN("GOOS","GOARCH","CGO_ENABLED","TMPDIR","TMP","TEMP","GOTMPDIR"))], outputs}
elif .file != null and (.file.path | test("/(stdlib\\.pkg\\.json|gohacks\\.(a|x)|(leave|inspect)\\.txt)$")) then
  {kind:"files", id, files:[.file]}
elif .directory != null and (.directory.path | endswith("/pkg")) then
  {kind:"files", id, archives:([.directory.files[]? | select(.path | endswith(".a"))]|length),
   files:[.directory as $d | $d.files[]? | select(.path|endswith("/runtime.a")) | .path=($d.path+"/"+.path)]}
else empty end
'''

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def save(name, data):
    (OUT / name).write_text(json.dumps(data, indent=2) + "\n")

def original_events(bep):
    summary = {"targets": {}}
    if not bep.is_file():
        return summary
    for line in bep.open():
        event = json.loads(line)
        id_ = event["id"]
        if "started" in event:
            summary["invocation"] = event["started"]["uuid"]
        if "finished" in event:
            summary["finish"] = event["finished"]["finishTime"]
            summary["exit_code"] = event["finished"]["exitCode"].get("code", 0)
        target = id_.get("targetCompleted", {})
        if target.get("label") in TARGETS and not target.get("aspect"):
            summary["targets"][target["label"]] = event.get("completed", {}).get("success", False)
    return summary

def actions(compact):
    spawns, blobs = [], {}
    with subprocess.Popen([str(TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(compact)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as reader:
        query = subprocess.run(["jq", "-c", FILTER], stdin=reader.stdout, stdout=subprocess.PIPE, check=True)
        reader.stdout.close()
        if reader.wait():
            raise ValueError("Compact log reader failed")
    for line in query.stdout.splitlines():
        item = json.loads(line)
        if item["kind"] == "files":
            blobs[item["id"]] = item
        else:
            spawns.append(item)
    for item in spawns:
        paths = [blobs[id_["outputId"]] for id_ in item.pop("outputs") if id_.get("outputId") in blobs]
        item["files"] = [file for group in paths for file in group["files"] if Path(file["path"]).name in OUTPUT_NAMES]
        item["archives"] = sum(group.get("archives", 0) for group in paths)
        item["properties"] = {k["name"]: k["value"] for k in item["properties"]}
        item["env"] = {k["name"]: k["value"] for k in item["env"]}
    return spawns

def native(row):
    props = row["properties"]
    return (props.get("Arch") == "arm64" and props.get("OSFamily") == "linux"
            and props.get("recycle-runner") == "false"
            and props.get("container-image") == IMAGE and props.get("workload-isolation-type") == "oci")


TEMP_NAMES = {"TMPDIR", "TMP", "TEMP", "GOTMPDIR"}
MISSING = "/fixture-missing-tmpdir"
OLD_CLANG = {"hash": "6cf0319226867844b8028a2f3393141dcbc82f1cd1d20410b60df9a7b4cd5696", "sizeBytes": "350"}


def positive():
    source = os.environ["GITHUB_SHA"]
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
    assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == BASE
    assert "rules_go_temp_env.patch" not in Path("MODULE.bazel").read_text()
    assert "TMPDIR" not in Path("tools/rules_go_bootstrap_shell.patch").read_text()
    assert not Path("tools/rules_go_owned_temp.patch").exists()
    bazel = ["bazel", "--bazelrc=" + str(TMP / "sidecar.bazelrc")]
    bep, compact, log = [TMP / ("worker" + suffix) for suffix in (".bep", ".compact", ".log")]
    row = {"source": source, "base": BASE, "version": "9.3.1", "started": now(), "verified": False}
    with log.open("w") as stream:
        row["native_exit"] = subprocess.run([*bazel, "build", *FLAGS,
            "--build_event_json_file=" + str(bep), "--execution_log_compact_file=" + str(compact),
            *TARGETS], stdout=stream, stderr=subprocess.STDOUT,
            env={k: v for k, v in os.environ.items() if k != "BUILDBUDDY_API_KEY"}).returncode
    row["finished"] = now()
    save("go.json", row)
    row["events"] = original_events(bep)
    row["actions"] = actions(compact)
    save("go.json", row)
    data = log.read_text(errors="replace")
    row["diagnostics"] = [kind for kind, pattern in {
        "missing_tmp": "stat /tmp: no such file or directory",
        "image_pull": r"(failed|unable) to (pull|resolve).*image",
        "permission_denied": "permission denied",
        "source_analysis": r"cannot load|patch FAILED",
    }.items() if re.search(pattern, data, re.I)]
    save("go.json", row)
    assert row["native_exit"] == row["events"].get("exit_code") == 0
    assert row["events"]["targets"] == dict.fromkeys(TARGETS, True)
    observed = {}
    for mnemonic, file in EXPECTED.items():
        label = PACKAGE if mnemonic == "GoCompilePkg" else "@@rules_go+//:stdlib"
        candidates = [a for a in row["actions"] if a["mnemonic"] == mnemonic and
                      (a["label"] == PACKAGE if label == PACKAGE else a["label"].endswith("//:stdlib"))]
        assert len(candidates) == 1
        a, = candidates
        assert native(a) and a["exit"] == 0 and a["runner"] == "remote" and not a["cache"]
        assert a["env"].get("GOOS") == "linux" and a["env"].get("GOARCH") == "arm64" and not TEMP_NAMES.intersection(a["env"])
        assert any(Path(f["path"]).name == file and int(f["digest"]["sizeBytes"]) > 0 for f in a["files"])
        if mnemonic == "GoStdlib":
            assert a["archives"] > 100
        observed[mnemonic] = a["digest"]
    for label in PROBES:
        a, = [a for a in row["actions"] if a["label"] == label and a["mnemonic"] == "ProbeWorkerTmp"]
        assert native(a) and a["exit"] == 0 and a["runner"] == "remote" and not a["cache"]
        name = label.split(":")[-1]
        data = Path("bazel-bin/test/rbe/go_stdlib_workdir/" + name + ".txt").read_bytes()
        assert len(a["files"]) == 1 and hashlib.sha256(data).hexdigest() == a["files"][0]["digest"]["hash"]
        lines = data.decode().splitlines()
        assert lines[:6] == [name, "aarch64", "mode=1777", "initially_empty=true", "ambient_tools=absent", "marker_absent_at_start=true"]
        row[name] = lines
    assert subprocess.run(["git", "diff", "--quiet", "HEAD"]).returncode == 0
    row.update(verified=True, verified_at=now())
    save("go.json", row)
    return observed["GoStdlibList"]


def raw_blob(digest):
    key = os.environ["BUILDBUDDY_API_KEY"]
    assert 0 < int(digest["sizeBytes"]) <= 4 * 1024 * 1024
    data = subprocess.run([str(TMP / "bb"), "download", digest["hash"] + "/" + str(digest["sizeBytes"]),
                           "--target=grpcs://remote.buildbuddy.io", "--api_key=" + key], capture_output=True, timeout=45)
    assert data.returncode == 0 and len(data.stdout) == int(digest["sizeBytes"])
    assert hashlib.sha256(data.stdout).hexdigest() == digest["hash"]
    return data.stdout


def replay(name, original):
    from cas_inputs import cas

    key = os.environ["BUILDBUDDY_API_KEY"]
    old = cas(original, "Action")
    command = cas(old["commandDigest"], "Command")
    root = old["inputRootDigest"]
    args = command["arguments"]
    props = {item["name"]: item["value"] for item in
             (old.get("platform") or command["platform"])["properties"]}
    env = {item["name"]: item.get("value", "") for item in command.get("environmentVariables", [])}
    paths = command.get("outputPaths") or command.get("outputFiles")
    assert not command.get("workingDirectory") and not command.get("outputDirectories") and len(paths) == 1
    assert not TEMP_NAMES.intersection(env) and props["Arch"] == "arm64" and props["workload-isolation-type"] == "oci"
    if name == "clang":
        assert props["container-image"].endswith("c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef")
        assert args[0].endswith("/bin/clang") and any(arg.endswith(".S") for arg in args) and paths[0].endswith(".o")
        props.update({"container-image": IMAGE, "recycle-runner": "false"})
    else:
        assert "stdliblist" in args and props["container-image"] == IMAGE and paths[0].endswith("/stdlib.pkg.json")
        env["TMPDIR"] = MISSING

    invocation = str(uuid.uuid4())
    response = TMP / (name + "-private-response.json")
    request = [str(TMP / "bb"), "execute", "--remote_executor=grpcs://remote.buildbuddy.io",
               "--remote_header=x-buildbuddy-api-key=" + key, "--invocation_id=" + invocation,
               "--input_root_digest=" + root["hash"] + "/" + str(root["sizeBytes"]),
               "--output=id", "--response_json_file=" + str(response), "--remote_timeout=2m",
               *["--exec_properties=" + k + "=" + v for k, v in sorted(props.items())],
               *["--action_env=" + k + "=" + v for k, v in sorted(env.items())],
               "--output_path=" + paths[0], "--", *args]
    row = {"verified": False, "original_action": original, "command_digest": old["commandDigest"],
           "input_root": root, "invocation": invocation, "executable": args[0], "output": paths[0],
           "architecture": "arm64", "image": IMAGE, "tmpdir": env.get("TMPDIR"), "started": now()}
    save(name + ".json", row)
    # bb's execution ID and raw RPC responses stay private to the coordinator.
    proc = subprocess.run(request, capture_output=True, timeout=180)
    row.update(client_exit=proc.returncode, finished=now())
    save(name + ".json", row)
    reply = json.loads(response.read_bytes())
    result = reply["result"]
    row.update(remote_exit=result.get("exitCode", 0), rpc=reply.get("status", {}).get("code", 0),
               cached=reply.get("cachedResult", False))
    save(name + ".json", row)
    timings = result.get("executionMetadata", {})
    row["execution"] = {k: timings.get(k) for k in ("executionStartTimestamp", "executionCompletedTimestamp")}
    save(name + ".json", row)
    assert all(row["execution"].values())
    assert row["rpc"] == 0 and not row["cached"]
    if name == "clang":
        file, = result["outputFiles"]
        assert row["client_exit"] == row["remote_exit"] == 0 and file["path"] == paths[0] and int(file["digest"]["sizeBytes"]) > 0
        elf = raw_blob(file["digest"])
        assert elf[:6] == b"\x7fELF\x02\x01" and elf[16:20] == b"\x01\x00\xb7\x00"
        row.update(output_digest=file["digest"], elf="AArch64 ELF64 relocatable")
    else:
        import base64
        outputs = []
        for stream in ("stderr", "stdout"):
            raw = base64.b64decode(result.get(stream + "Raw", ""))
            digest = result.get(stream + "Digest", {})
            if not raw and int(digest.get("sizeBytes", 0)):
                raw = raw_blob(digest)
            outputs.append(raw)
        assert row["remote_exit"] != 0 and any(
            MISSING.encode() in raw and b"no such file or directory" in raw.lower() for raw in outputs
        )
        row["diagnostic"] = "configured absent TMPDIR: no such file or directory"
    row.update(verified=True, verified_at=now())
    save(name + ".json", row)


def main():
    OUT.mkdir(exist_ok=True)
    image = OUT / "image.json"
    checks = {"image": image.is_file() and json.loads(image.read_bytes())["manifest"] == IMAGE.split("@")[1]}
    for name in ("go", "clang", "missing"):
        try:
            if name == "go":
                stdlib_list = positive()
            elif name == "clang":
                replay(name, OLD_CLANG)
            elif checks.get("go") is True:
                replay(name, stdlib_list)
            else:
                raise ValueError("the positive command is unavailable")
            checks[name] = True
        except Exception as exc:
            import traceback
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            checks[name] = {"error": type(exc).__name__, "line": frame.lineno, "function": frame.name}
        save("verdict.json", {"checks": checks, "at": now()})
    return 0 if all(value is True for value in checks.values()) else 1


def missing_only():
    import traceback
    import zipfile

    archive = Path(__file__).with_name("original-worker.zip")
    original_hash = "5a1c9e15ff181aae86d4777c70b8c5b1e5ed05c19534d45c9395301aef27e480"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == original_hash
    OUT.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as original:
        assert set(original.namelist()) == {"go.json", "image.json", "clang.json", "verdict.json"}
        for name in original.namelist():
            (OUT / ("original-verdict.json" if name == "verdict.json" else name)).write_bytes(original.read(name))
    go = json.loads((OUT / "go.json").read_bytes())
    clang = json.loads((OUT / "clang.json").read_bytes())
    assert go["source"] == "5850173fce71dacda14cbcf178f9cc69c689806d"
    assert go["verified"] and go["native_exit"] == 0 and clang["verified"] and clang["remote_exit"] == 0
    assert go["events"]["invocation"] == "102c65bc-4577-4ed9-a955-f4f06ececcce"
    selected, = [a for a in go["actions"] if a["label"] == "@@rules_go+//:stdlib" and a["mnemonic"] == "GoStdlibList"]
    assert selected["digest"] == {"hash": "b23747b97ea69834a7ad2dc216c3ef33a3aa3faf31ba174d8ab63a958f5c47dd", "sizeBytes": "376"}
    assert native(selected) and selected["runner"] == "remote" and not selected["cache"] and selected["exit"] == 0
    row = {"verified": False, "original_archive": original_hash, "original_artifact": 13098220759,
           "original_run": 41342268569, "started": now()}
    try:
        replay("missing", selected["digest"])
        row["verified"] = True
    except Exception as exc:
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        row["error"] = {"class": type(exc).__name__, "line": frame.lineno, "function": frame.name}
    row["verified_at"] = now()
    save("verdict.json", row)
    return 0 if row["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(missing_only() if sys.argv[1:] == ["--missing-only"] else main())
