"""Keep the three configured worker records, not the coordinator's shell output."""

import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tarfile

ROOTS = {
    "PackageTar": ("payload.tar", "3a57a5fe4121f867ccd0c26c89c511dd0f5851712ae6723babed02f950193898", 10240),
    "MakeDeb": ("gvisor-writer-input_1_all.deb", "486d93a4ea624b12536af762811caeac49ab801c05169c7dc744d737673ab015", 572),
    "RelocatedPythonWriter": ("relocated.json", None, None),
}
WRITERS = {"PackageTar": "build_tar", "MakeDeb": "make_deb"}
PYTHON = "6fd428f3883d8b660ab82562603e5d4adbf45349dd2c2ea77e87e1a4de567557"
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"


def digest(b):
    return hashlib.sha256(b).hexdigest()


def decoded_rows(text):
    decode = json.JSONDecoder().raw_decode
    while text.strip():
        text = text.lstrip()
        row, length = decode(text)
        yield row
        text = text[length:]


def inspect(result):
    temp = Path(os.environ["RUNNER_TEMP"])
    with tarfile.open(temp / "qualification.tar.gz") as archive:
        assert archive.extractfile("final-head.txt").read().decode().strip() == os.environ["GITHUB_SHA"]
        events, = (n for n in archive.getnames() if n.endswith("/events.jsonl"))
        events = [json.loads(line) for line in archive.extractfile(events)]
        result["invocation"], = (event["started"]["uuid"] for event in events if "started" in event)
        path, = (n for n in archive.getnames() if n.endswith("/native-exit.txt"))
        result["native_exit"] = int(archive.extractfile(path).read())
        path, = (n for n in archive.getnames() if n.endswith("/execution.binpb"))
        record = archive.extractfile(path).read()
        result["execution_log"] = {"sha256": digest(record), "bytes": len(record)}
        compact = temp / "native-launcher.binpb"
        compact.write_bytes(record)
    printed = subprocess.check_output([temp / "bb", "print", "--compact_execution_log=" + str(compact)], text=True)
    records = result["workers"] = {}
    for row in decoded_rows(printed):
        key = row.get("mnemonic")
        if key not in ROOTS:
            continue
        assert key not in records, key
        props = {p["name"]: p["value"] for p in row["platform"]["properties"]}
        rec = records[key] = {"target": row["targetLabel"], "action": row["digest"], "runner": row["runner"],
                              "cache_hit": row.get("cacheHit", False), "exit": row.get("exitCode", 0),
                              "image": props.get("container-image"), "cpu": props.get("Arch")}
        assert rec["runner"] == "remote" and not rec["cache_hit"] and rec["cpu"] == "arm64" and rec["image"] == IMAGE
        args = row["commandArgs"]
        files = {r["path"]: r.get("digest", {}) for r in row["inputs"]}
        if key in WRITERS:
            program = args[0]
            assert program.endswith("/" + WRITERS[key]), program
            launcher = Path(program).read_bytes()
            assert launcher[:5] == b"\x7fELF\x02" and int.from_bytes(launcher[18:20], "little") == 183
            stage1 = Path(program + ".bootstrap.py").read_bytes()
            assert b"python_bootstrap_template.txt" in stage1 and b"find_runfiles_root" in stage1, program
            rec["python_bootstrap_sha256"] = digest(stage1)
            runfiles = Path(program + ".runfiles")
            runtime = runfiles / "+_repo_rules+protoc_check_static_python_arm64"
            # Walk the real materialized runtime: distinguish common files from
            # the native binary and the target-specific generated bootstrap.
            runtime_files = {}
            for folder, _, files in os.walk(runtime, followlinks=True):
                for file in files:
                    p = Path(folder) / file
                    content = p.read_bytes()
                    runtime_files[str(p.relative_to(runtime))] = (digest(content), len(content))
            interpreter = runtime_files["bin/python3.11"]
            assert interpreter == (PYTHON, 44747056), interpreter
            assert len(runtime_files) > 400, len(runtime_files)
            unique = dict(runtime_files.values())
            rec["shared_runtime"] = {
                "interpreter": list(interpreter),
                "logical_file_count": len(runtime_files),
                "content_bytes": sum(unique.values()),
                "file_set_digest": digest(json.dumps(runtime_files, sort_keys=True).encode()),
            }
            rec["expanded_bootstrap_bytes"] = len(stage1)

            off = struct.unpack_from("<Q", launcher, 32)[0]
            size, count = struct.unpack_from("<HH", launcher, 54)
            assert all(struct.unpack_from("<I", launcher, off + i * size)[0] != 3 for i in range(count))
            assert files[program]["hash"] == digest(launcher) and int(files[program]["sizeBytes"]) == len(launcher)
            rec["executable"] = [program, digest(launcher), len(launcher)]
        else:
            assert files[args[0]]["hash"] == PYTHON and int(files[args[0]]["sizeBytes"]) == 44747056
        if rec["exit"]:
            continue
        name, expected, size = ROOTS[key]
        output, = (o for o in row["actualOutputs"] if o["path"].endswith("/" + name))
        content = Path(output["path"]).read_bytes()
        sha = digest(content)
        assert sha == output["digest"]["hash"] and len(content) == int(output["digest"]["sizeBytes"])
        assert any(f.get("digest") == sha and int(f.get("length", 0)) == len(content)
                   for e in events for f in e.get("namedSetOfFiles", {}).get("files", []))
        rec["output"] = [name, sha, len(content)]
        if expected:
            assert (expected, size) == (sha, len(content))
        else:
            result["manifest"] = json.loads(content)
    relocated_only = os.environ["GITHUB_REF_NAME"].startswith("qualification/rules-python-native-launcher-4183-relocated")
    expected = {"RelocatedPythonWriter"} if relocated_only else set(ROOTS)
    assert result["native_exit"] == 0 and set(records) == expected and not any(r["exit"] for r in records.values())
    assert records["PackageTar"]["shared_runtime"] == records["MakeDeb"]["shared_runtime"]
    manifest = result["manifest"]
    if relocated_only:
        # Exact original PackageTar input from the first, already successful worker.
        result["package_invocation"] = "813b647f-0118-4b22-9e90-4fadd8dae6c3"
        writer = "b4e39567888f7f6e390260950a6cdf89bae5f2b4dda1c0b1dd4450962f5c8922"
    else:
        writer = records["PackageTar"]["executable"][1]
    assert manifest["interpreter_importtime"] and (manifest["independent_roots_exit"] == 0 or manifest["independent_roots_adapter_missing_root"]), manifest
    assert manifest["writer"] == writer and manifest["interpreter"] == PYTHON
    assert manifest["missing_prefix_exit"] != 0 and manifest["relocated_exit"] == 0 and len(manifest["manifest_prefixes"]) >= 2


result = {"source": os.environ["GITHUB_SHA"]}
try:
    inspect(result)
    result["status"] = "PASS"
finally:
    (Path(os.environ["RUNNER_TEMP"]) / "native-launcher.json").write_text(json.dumps(result, indent=2) + "\n")
