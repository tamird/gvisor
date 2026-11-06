"""Check the public Go export producer and every byte of its source archive."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import uuid
import zipfile

SOURCE = "5dfd4dd0b09294681439927f8cd39ee2debad9b3"
stage = Path(os.environ["RUNNER_TEMP"]) / "go-export"
worker = "//test/rbe/release_prewarmer:amd64"
properties = {"Arch": "amd64", "OSFamily": "linux", "workload-isolation-type": "oci", "recycle-runner": "false",
              "container-image": "docker://gcr.io/distroless/static-debian13@sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"}
target = "//:go_export"
receipt = {"source": SOURCE, "worker": properties, "invocation": str(uuid.uuid4())}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save():
    (stage / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")


def runfiles_path(raw):
    assert not Path(raw).is_absolute() and ".." not in Path(raw).parts
    return Path(raw)


def archive(args):
    assert args[1] == "--gopath" and args[3] == "--output" and args[5] == "--go-mod"
    mod = runfiles_path(args[6])
    module = re.search(r"^module\s+(\S+)", mod.read_text(), re.M)[1]
    prefix = "src/" + module + "/"
    hashes = {}
    with zipfile.ZipFile(runfiles_path(args[2])) as inputs:
        for entry in inputs.infolist():
            if entry.filename.startswith(prefix) and not entry.is_dir():
                hashes[entry.filename[len(prefix):]] = sha(inputs.read(entry))
    receipt["gopath_module_files"] = len(hashes)
    for arg in (args[6], *args[7:]):
        path = runfiles_path(arg)
        hashes[path.relative_to(mod.parent).as_posix()] = sha(path.read_bytes())
    readme = re.search(r"const readme = `([^`]*)`", Path("tools/go_export/main.go").read_text(), re.S)
    assert readme is not None
    hashes["README.md"] = sha(readme[1].encode())
    out = runfiles_path(args[4])
    with zipfile.ZipFile(out) as output:
        names = output.namelist()
        assert sorted(hashes) == names
        for name in names:
            assert sha(output.read(name)) == hashes[name], name
    receipt["archive"] = {"entries": len(names), "sha256": sha(out.read_bytes()),
                          "sidecar": "runsc/fdparking/fdparking.c" in names,
                          "generated_state": any(n.endswith("_state_autogen.go") for n in names)}
    assert receipt["gopath_module_files"] > 1000 and receipt["archive"]["sidecar"] and receipt["archive"]["generated_state"]


def inspect_elf(name):
    path = runfiles_path(name)
    if path.is_file():
        raw = path.read_bytes()
        assert raw[:6] == b"\x7fELF\x02\x01"
        machine = struct.unpack_from("<H", raw, 18)[0]
        table = struct.unpack_from("<Q", raw, 32)[0]
        stride, count = struct.unpack_from("<HH", raw, 54)
        assert stride >= 56 and count and table + stride * count <= len(raw)
        interp = None
        for off in range(table, table + stride * count, stride):
            if struct.unpack_from("<I", raw, off)[0] == 3:
                start = struct.unpack_from("<Q", raw, off + 8)[0]
                length = struct.unpack_from("<Q", raw, off + 32)[0]
                assert 1 < length < 256 and start + length <= len(raw)
                value = raw[start:start+length]
                assert re.fullmatch(rb"/[A-Za-z0-9_./+-]+\x00", value) and interp is None
                interp = value[:-1].decode()
        receipt["assembler"] = {"sha256": sha(raw), "path": name, "machine": machine, "interpreter": interp}


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    assert sha(Path("test/rbe/release_prewarmer/BUILD").read_bytes()) == "ae1f23a11f3cb747fbec49a34c510c5ab9dcb75d06d94bfd4d9a49d50c629271"
    bazel = [str(stage/"bazel"), "--nosystem_rc", "--nohome_rc", "--bazelrc="+str(stage/"private.bazelrc")]
    flags = ["--config=x86_64", "-c", "opt",
             "--extra_execution_platforms="+worker, "--host_platform="+worker,
             "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
             "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
             "--@rules_python//python/config_settings:bootstrap_impl=native", "--lockfile_mode=off",
             "--jobs=40", "--color=no", "--curses=no", "--noshow_progress"]
    save()
    result = subprocess.run([*bazel, "aquery", *flags, "--output=jsonproto", "--noinclude_artifacts",
                             'mnemonic("GoExport", //:go_export)'], capture_output=True)
    receipt["analysis_exit"] = result.returncode
    receipt["analysis_stderr_sha256"] = sha(result.stderr)
    save()
    result.check_returncode()
    graph = json.loads(result.stdout)
    actions = [a for a in graph["actions"] if a["mnemonic"] == "GoExport"]
    assert len(actions) == 1
    args = actions[0]["arguments"]
    assert args[0].startswith("bazel-out/") and args[0].endswith("/tools/go_export/assemble_/assemble")
    receipt["configured_tool"] = args[0]
    save()
    with (stage/"build.log").open("wb") as log:
        proc = subprocess.run(["timeout", "--kill-after=15", "2400", *bazel, "build", *flags, "--keep_going",
                               "--invocation_id="+receipt["invocation"], "--build_event_json_file="+str(stage/"bep"),
                               "--execution_log_compact_file="+str(stage/"exec.zst"),
                               "--modify_execution_info=^GoExport$=+no-remote-cache",
                               "--remote_download_outputs=toplevel", "--remote_download_regex=.*/tools/go_export/assemble_/assemble$",
                               "//:gopath", target], stdout=log, stderr=subprocess.STDOUT)
    receipt["build_exit"] = proc.returncode
    receipt["build_log_sha256"] = sha((stage/"build.log").read_bytes())
    save()
    if (stage/"bep").is_file():
        failures = []
        for line in (stage/"bep").open():
            event = json.loads(line)
            if "finished" in event:
                receipt["bazel_exit"] = event["finished"]["exitCode"].get("code", 0)
            ac = event.get("action", {})
            key = event.get("id", {}).get("actionCompleted")
            if key and not ac.get("success", False) and len(failures) < 8:
                owner = key.get("label", "")
                mnemonic = ac.get("type", "")
                assert re.fullmatch(r"[@/\w:+.~@-]*", owner) and re.fullmatch(r"[\w-]*", mnemonic)
                failures.append({"owner": owner, "mnemonic": mnemonic, "exit": ac.get("exitCode")})
        receipt["failed_actions"] = failures
    if (stage/"exec.zst").is_file():
        expr = """select(.spawn) | .spawn | select(.mnemonic=="GoExport" or ((.exitCode//0)!=0)) | [.targetLabel,.mnemonic,.args[0],.runner,(.cacheHit//false),(.exitCode//0),([.platform.properties[]?|{key:.name,value:.value}]|from_entries|contains($p))]"""
        with subprocess.Popen([stage/"bb", "print", "--raw", "--compact_execution_log="+str(stage/"exec.zst")], stdout=subprocess.PIPE) as reader:
            projected = subprocess.run(["jq", "-c", "--argjson", "p", json.dumps(properties), expr], stdin=reader.stdout, capture_output=True, text=True, check=True)
            assert reader.wait()==0
        rows = []
        for line in projected.stdout.splitlines():
            owner, mnemonic, exe, runner, cached, code, matched = json.loads(line)
            assert all(re.fullmatch(r"[@/\w:+.~@-]*", v or "") for v in (owner, mnemonic, runner))
            if len(rows)<8 or mnemonic=="GoExport":
                rows.append({"owner": owner, "mnemonic": mnemonic, "runner": runner, "cached": cached, "exit": code, "worker_matches": matched})
            if owner == target and mnemonic == "GoExport":
                assert exe == args[0]
        receipt["executions"] = rows
    inspect_elf(args[0])
    save()
    assert receipt["build_exit"] == receipt.get("bazel_exit") == 0, "inspect original failing action"
    assert any(row == {"owner": target, "mnemonic": "GoExport", "runner": "remote", "cached": False, "exit": 0, "worker_matches": True} for row in receipt["executions"])
    archive(args)
    save()
    assert receipt["assembler"]["machine"] == 62 and receipt["assembler"]["interpreter"] is None


try:
    main()
finally:
    save()
