"""Recover the original compiler failure, then use the released LLVM toolchain."""

import ast
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from zipfile import ZipFile

from cas_inputs import blob, cas, rpc
from external_go_policy import AREA, SOURCE, client, details, git, now, read_events

SPLIT = "03763b0448f9e15a2247ba0a50e8b710cf4ba0e9"
PRODUCT = "c734e2b4f7b776258294c4d5c2f508d6c6ec994e"
INV = "0930b963-0517-4597-a33e-ef5d83b52ad4"
BASE = "@@protobuf+//upb/base:base"
ABSL = "@@abseil-cpp+//absl/base:base"
IMAGE = "docker://" + os.environ["GNU"]
OUT = AREA.parent / "external-protoc.json"
RESULT = {"verdict": "INCOMPLETE", "source": PRODUCT, "control_source": SPLIT, "phases": {},
          "prior": {"run": 41406932259, "artifact": 13123603522, "verdict": "INCOMPLETE",
                    "sha256": "7661b58198ee4c2de44a3cd46f406023634508682b2e058d79332de0bf5b1bf4"}}
BAZEL = client("8.8.1")


def save():
    OUT.write_text(json.dumps(RESULT, indent=2) + "\n")


def phase(name, fn):
    row = RESULT["phases"][name] = {"status": "INCOMPLETE", "started": now()}
    save()
    try:
        fn(row)
    except Exception as exc:
        # Keep the original process disposition if later collection fails.
        row["collection_error"] = type(exc).__name__
        tb = exc.__traceback__
        while tb.tb_next:
            tb = tb.tb_next
        row["collection_site"] = f"{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}"
    row["finished"] = now()
    save()


def invocation_log(invocation):
    chunks, token = [], "0"
    for _ in range(16):
        response = rpc("GetEventLogChunk", {"invocationId": invocation, "chunkId": token, "minLines": 100000})
        data = base64.b64decode(response.get("buffer", ""), validate=True)
        chunks.append(data)
        assert sum(map(len, chunks)) <= 16 * 1024 * 1024
        next_token = response.get("nextChunkId", "")
        if not data or not next_token:
            return b"".join(chunks).decode()
        assert next_token != token
        token = next_token
    raise ValueError("Original log exceeded the bounded recovery")


def original(row):
    row["download_exit"] = int((AREA / "original-download-exit").read_text())
    assert row["download_exit"] == 0
    data = (AREA / "original.zip").read_bytes()
    row["archive_sha256"] = sha256(data).hexdigest()
    assert row["archive_sha256"] == "b924c06570792e9732e95a43a786eb9933f8b6d862edcda4fce74466628c11e7"
    with ZipFile(AREA / "original.zip") as z:
        assert z.namelist() == ["external-go.json"]
        old = json.loads(z.read("external-go.json"))["builds"]["ordinary"]
    assert (old["source"], old["invocation"], old["state"], old["exit"]) == (SPLIT, INV, "BUILD_FAILED", 1)
    row.update(invocation=INV, original_exit=1, original_state=old["state"], actions=[])
    log = invocation_log(INV)
    row["log_sha256"] = sha256(log.encode()).hexdigest()
    log = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", log)
    row["absolute_headers"] = sorted(set(re.findall(r"['\"](/usr/(?:include|lib)/[A-Za-z0-9_./+-]+)['\"]", log)))
    row["inclusion_owners"] = sorted(set(re.findall(r"absolute path inclusion\(s\) found in rule ['\"]([^'\"]+)['\"]", log)))
    row["cxx_standard_diagnostics"] = sorted(set(re.findall(r"[^\n]*C\+\+ versions less than C\+\+17[^\n]*", log)))
    save()
    executions = rpc("GetExecution", {"executionLookup": {"invocationId": INV},
                                       "inlineExecuteResponse": True}).get("execution", [])
    for owner in (BASE, ABSL):
        matches = [e for e in executions if e.get("targetLabel") == owner and e.get("actionMnemonic") == "CppCompile"]
        assert matches, owner
        e = sorted(matches, key=lambda a: a.get("primaryOutputPath", ""))[0]
        action = cas(e["actionDigest"], "Action")
        command = cas(action["commandDigest"], "Command")
        args = command["arguments"]
        assert args and len(json.dumps(args)) < 32768 and all(not re.search(r"https?://|api.key|authorization", a, re.I) for a in args)
        row["actions"].append({"owner": owner, "action": e["actionDigest"], "command": action["commandDigest"],
                               "arguments": args, "platform": command.get("platform"),
                               "stage": e.get("stage"), "status_code": e.get("status", {}).get("code", 0),
                               "remote_exit": e.get("exitCode", 0),
                               "cached": e.get("executeResponse", {}).get("cachedResult", False)})
        save()
    assert row["absolute_headers"] and BASE in row["inclusion_owners"]
    row["status"] = "RECOVERED"


def prepare():
    assert git("rev-parse", "HEAD") == PRODUCT and not git("status", "--porcelain", "--untracked-files=no")
    work = AREA / "consumer"
    work.mkdir()
    patch = (AREA / "gazelle.patch").read_bytes()
    assert sha256(patch).hexdigest() == "02dbcbd8f45ef86ea3a9ce08eed9d88dd27a1d7493e9f5be0c80937b6b43a77e"
    (work / "gazelle.patch").write_bytes(patch)
    module = ('module(name="consumer")\nbazel_dep(name="gvisor",version="0.0.1")\n'
              f'local_path_override(module_name="gvisor",path={json.dumps(str(SOURCE))})\n'
              'bazel_dep(name="llvm",version="0.8.24")\nregister_toolchains("@llvm//toolchain:all")\n'
              'bazel_dep(name="protobuf",version="35.1")\n'
              'single_version_override(module_name="gazelle",version="0.51.3",patches=["//:gazelle.patch"],patch_strip=1)\n')
    (work / "MODULE.bazel").write_text(module)
    (work / "BUILD.bazel").write_text('exports_files(["gazelle.patch"])\n'
        'alias(name="shim",actual="@gvisor//shim:containerd-shim-runsc-v1")\n'
        'alias(name="extension",actual="@gvisor//pkg/shim/v1/extension:extension")\n'
        'alias(name="protoc",actual="@protobuf//:protoc")\n')
    RESULT["consumer"] = {"module": module.replace(str(SOURCE), "<gvisor-checkout>"),
                          "module_sha256": sha256(module.encode()).hexdigest(), "gazelle_patch_sha256": sha256(patch).hexdigest(),
                          "gvisor_rc_loaded": False, "prefer_prebuilt_protoc": False}
    save()
    return work


def flags():
    return ["--jobs=24", "--keep_going", "--remote_download_outputs=toplevel",
            *["--remote_default_exec_properties=" + k + "=" + v for k, v in {
                "Arch": "amd64", "OSFamily": "linux", "EstimatedMemory": "2GB",
                "workload-isolation-type": "firecracker", "container-image": IMAGE}.items()]]


def prior():
    data = (AREA / "selection.zip").read_bytes()
    assert sha256(data).hexdigest() == "bf8b3f83acb1bab78a78a107da3575a43f4cce7a418be0f57c906a43157d1ebf"
    with ZipFile(AREA / "selection.zip") as z:
        assert z.namelist() == ["external-protoc.json"]
        result = json.loads(z.read(z.namelist()[0]))
    assert result["source"] == PRODUCT and result["control_source"] == SPLIT
    return result


def control_diagnostic(row):
    previous = prior()["phases"]["control_preflight"]
    assert previous["exit"] == 1 and previous["source"] == SPLIT
    assert previous["invocation"] == "9c642ca9-e3b9-44d3-9089-3baf6ab4b069"
    row["original_record"] = previous
    log = invocation_log(previous["invocation"])
    row.update(log_sha256=sha256(log.encode()).hexdigest(), diagnostics=details(log))
    save()
    assert "configure" in "\n".join(row["diagnostics"]) and "kernel_headers" in "\n".join(row["diagnostics"])
    row["status"] = "RECOVERED"


def selection(row):
    previous = prior()
    assert RESULT["consumer"] == previous["consumer"]
    assert git("rev-parse", "HEAD") == PRODUCT
    # Reuse the completed query only with identical build options and consumer.
    old = git("show", "ad92caae641fb95f62621a26042552f71a6cfa01:test/rbe/external_protoc.py")
    current = Path(__file__).read_text()
    def flags_body(text):
        return ast.dump(next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == "flags"))
    assert flags_body(old) == flags_body(current)
    p = previous["phases"]["preflight"]
    assert p["status"] == "PASS" and p["exit"] == 0 and p["source"] == PRODUCT
    assert p["invocation"] == "d8f0e6a6-0e71-4be3-8969-059c561595c8"
    row.update(status="PASS", original_record=p, reused_from_artifact=13123479492)


def build(row, work):
    bep, spawns = AREA / "build.bep", AREA / "build.spawns"
    with (AREA / "build.log").open("w") as log:
        p = subprocess.run([*BAZEL, "build", *flags(), "--build_event_json_file=" + str(bep),
                            "--execution_log_json_file=" + str(spawns), "//:protoc", "//:shim", "//:extension"],
                           cwd=work, stdout=log, stderr=subprocess.STDOUT)
    row.update(exit=p.returncode, returned=now(), status="FAIL" if p.returncode else "INCOMPLETE", collection={})
    save()
    log = (AREA / "build.log").read_text(errors="replace")
    row.update(log_sha256=sha256(log.encode()).hexdigest(), errors=details(log) if p.returncode else [])
    completed = {}
    for e in read_events(bep, row["collection"]):
        if "started" in e:
            row.update(invocation=e["started"]["uuid"], bazel=e["started"]["buildToolVersion"])
        if "finished" in e:
            row["bazel_exit"] = e["finished"]["exitCode"].get("code", 0)
        if "targetCompleted" in e.get("id", {}):
            completed[e["id"]["targetCompleted"]["label"]] = e.get("completed", {}).get("success", False)
    row.update(completed=completed, compiles=[], links=[], proto_commands=[], failures=[])
    save()
    filt = '''select((.exitCode // 0) != 0 or
        (.targetLabel == "@@protobuf+//upb/base:base" or .targetLabel == "@@abseil-cpp+//absl/base:base") or
        ((.mnemonic == "CppLink" or .mnemonic == "GoLink") and (.targetLabel | test("protoc$|containerd-shim-runsc-v1$"))) or
        ((.mnemonic == "GenProtoDescriptorSet" or .mnemonic == "GoProtocGen") and
          (.targetLabel | contains("com_github_containerd_")))) |
        {targetLabel,mnemonic,commandArgs,exitCode,runner,cacheHit,platform,actualOutputs,
         inputs: [.inputs[]? | select(.path | contains("glibc_headers_include_search_directory") or
             contains("libcxx_headers") or endswith("/clang") or endswith("/clang++") or
             endswith("/protoc") or endswith(".params"))]}'''
    with subprocess.Popen(["jq", "-c", filt, str(spawns)], stdout=subprocess.PIPE, text=True) as proc:
        for line in proc.stdout:
            a = json.loads(line)
            entry = {"owner": a["targetLabel"], "mnemonic": a["mnemonic"], "exit": a.get("exitCode", 0),
                     "runner": a.get("runner"), "cached": a.get("cacheHit", False),
                     "platform": a.get("platform"), "outputs": a.get("actualOutputs"), "arguments": a.get("commandArgs")}
            if entry["exit"]:
                row["failures"].append({k: entry[k] for k in ("owner", "mnemonic", "exit", "runner", "cached")})
            if a["mnemonic"] == "CppCompile" and a["targetLabel"] in (BASE, ABSL):
                entry["selected_inputs"] = a.get("inputs")
                row["compiles"].append(entry)
            elif a["mnemonic"] in ("CppLink", "GoLink"):
                row["links"].append(entry)
            elif a["mnemonic"] in ("GenProtoDescriptorSet", "GoProtocGen"):
                entry["selected_inputs"] = a.get("inputs")
                row["proto_commands"].append(entry)
        row["collection"]["spawns_exit"] = proc.wait()
    row["source_unchanged"] = git("rev-parse", "HEAD") == PRODUCT and not git("status", "--porcelain", "--untracked-files=no")
    save()
    if p.returncode:
        return
    assert row["bazel_exit"] == 0 and row["bazel"] == "8.8.1" and row["source_unchanged"]
    assert completed == {"//:protoc": True, "//:shim": True, "//:extension": True}, completed
    assert row["collection"] == {".bep": "complete", "spawns_exit": 0} and not row["failures"]
    assert {a["owner"] for a in row["compiles"]} == {BASE, ABSL}
    for a in row["compiles"]:
        props = {v["name"]: v["value"] for v in a["platform"]["properties"]}
        assert not a["exit"] and (a["cached"] or a["runner"] == "remote") and props["container-image"] == IMAGE
        assert props["Arch"] == "amd64" and props["OSFamily"] == "linux"
        assert props["EstimatedMemory"] == "2GB" and props["workload-isolation-type"] == "firecracker"
        assert "/clang" in a["arguments"][0] and a["arguments"][0].startswith(("external/", "bazel-out/"))
    row["status"] = "PASS"


def outputs(row, work):
    prior = RESULT["phases"]["build"]
    row["binaries"] = {}
    row["source_protoc_consumers"] = []
    for generation in prior["proto_commands"]:
        args = generation["arguments"]
        for arg in args[:]:
            if arg.startswith("-param="):
                files = [f for f in generation["selected_inputs"] if f["path"] == arg[len("-param="):]]
                assert len(files) == 1
                args = args + shlex.split(blob(files[0]["digest"]).decode())
        for f in generation["selected_inputs"]:
            if Path(f["path"]).name != "protoc":
                continue
            links = [a for a in prior["links"] if a["mnemonic"] == "CppLink"
                     and any(o["path"] == f["path"] and o["digest"] == f["digest"] for o in a["outputs"] or [])]
            assert len(links) == 1 and f["path"] in args
            assert not generation["exit"] and (generation["cached"] or generation["runner"] == "remote")
            assert not links[0]["exit"] and (links[0]["cached"] or links[0]["runner"] == "remote")
            row["source_protoc_consumers"].append({"owner": generation["owner"], "mnemonic": generation["mnemonic"],
                "path": f["path"], "digest": f["digest"], "cpp_link_owner": links[0]["owner"],
                "generation_cached": generation["cached"], "link_cached": links[0]["cached"]})
    save()
    # The separately qualified Gazelle default can eliminate code generation.
    # In that case these three requested builds do not prove a protoc invocation
    # by the shim; retain that distinction instead of inventing a consumer.
    row["source_protoc_used_by_containerd"] = bool(row["source_protoc_consumers"])
    for target, suffix, args, expected in (("//:protoc", "protoc", ["--version"], "libprotoc 35.1"),
            ("//:shim", "containerd-shim-runsc-v1", ["-v"], "containerd-shim-runsc-v1:")):
        p = subprocess.run([*BAZEL, "cquery", *flags(), "--output=files", target], cwd=work, capture_output=True, text=True)
        paths = [s for s in p.stdout.splitlines() if s.startswith("bazel-out/") and Path(s).name == suffix]
        assert p.returncode == 0 and len(paths) == 1, (target, p.returncode, paths)
        path = work / paths[0]
        data = path.read_bytes()
        digest = sha256(data).hexdigest()
        owners = [(a, f) for a in prior["links"] for f in a["outputs"] or [] if f["path"] == paths[0]]
        assert len(owners) == 1
        a, f = owners[0]
        assert not a["exit"] and (a["cached"] or a["runner"] == "remote")
        props = {v["name"]: v["value"] for v in a["platform"]["properties"]}
        assert props["container-image"] == IMAGE and props["Arch"] == "amd64" and props["OSFamily"] == "linux"
        assert f["digest"]["hash"] == digest and int(f["digest"]["sizeBytes"]) == len(data)
        assert data[:4] == b"\x7fELF" and int.from_bytes(data[18:20], "little") == 62
        p = subprocess.run([str(path), *args], capture_output=True, text=True, timeout=30)
        if p.returncode:
            row["status"] = "FAIL"
        row["binaries"][target] = {"path": paths[0], "sha256": digest, "size": len(data),
                                    "owner": a["owner"], "exit": p.returncode, "expected_output": expected in p.stdout,
                                    "output_sha256": sha256((p.stdout + p.stderr).encode()).hexdigest()}
        save()
        assert p.returncode == 0 and expected in p.stdout
    row["status"] = "PASS"


def main():
    benign = "ERROR: linux/vfio.h: header not found"
    assert details(benign) == [benign]
    assert details("ERROR: --remote_header=x-buildbuddy-api-key=REDACTED") == []
    assert details("ERROR: Authorization: Bearer REDACTED") == []
    RESULT["diagnostic_projection_controls"] = "PASS"
    phase("original", original)
    work = prepare()
    phase("control_preflight", control_diagnostic)
    phase("preflight", selection)
    # Only a verified declared compiler justifies building the full consumer.
    if RESULT["phases"]["preflight"]["status"] == "PASS":
        phase("build", lambda row: build(row, work))
        phase("outputs", lambda row: outputs(row, work))
    RESULT["verdict"] = "PASS" if len(RESULT["phases"]) == 5 and all(
        r["status"] in ("PASS", "RECOVERED", "EXPECTED_REJECTION") for r in RESULT["phases"].values()) else "INCOMPLETE"
    if any(r["status"] == "FAIL" for r in RESULT["phases"].values()):
        RESULT["verdict"] = "FAIL"
    RESULT["finished"] = now()
    save()
    return RESULT["verdict"] != "PASS"


if __name__ == "__main__":
    raise SystemExit(main())
