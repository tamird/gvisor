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
PYTHON = "02939080d36c0fd2dc2507ff146a4c86360fa3968f30e1e42a6424d352629394"
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
        assert rec["runner"] == "remote" and not rec["cache_hit"] and rec["cpu"] == "amd64" and rec["image"] == IMAGE
        args = row["commandArgs"]
        files = {r["path"]: r.get("digest", {}) for r in row["inputs"]}
        if key in WRITERS:
            program = args[0]
            assert program.endswith("/" + WRITERS[key]), program
            launcher = Path(program).read_bytes()
            assert launcher[:5] == b"\x7fELF\x02" and int.from_bytes(launcher[18:20], "little") == 62
            off = struct.unpack_from("<Q", launcher, 32)[0]
            size, count = struct.unpack_from("<HH", launcher, 54)
            assert all(struct.unpack_from("<I", launcher, off + i * size)[0] != 3 for i in range(count))
            assert files[program]["hash"] == digest(launcher) and int(files[program]["sizeBytes"]) == len(launcher)
            rec["executable"] = [program, digest(launcher), len(launcher)]
        else:
            assert files[args[0]]["hash"] == PYTHON and int(files[args[0]]["sizeBytes"]) == 43532776
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
    manifest = result["manifest"]
    assert relocated_only and manifest["interpreter"] == PYTHON
    assert manifest["writer_size"] > 0 and manifest["zip_size"] > 0
    assert manifest["missing_prefix_exit"] != 0
    assert manifest["relocated_exit"] == manifest["independent_roots_exit"] == 0
    assert len(manifest["manifest_prefixes"]) >= 2
    assert manifest["relocated_archive"] == "36d353b3a4c24813e58fdba4825d932e24f29d393ea62c7c2cba7b89cc861eaf"
    assert manifest["payload"] == "28c61a191487fa5f514a655146fed81b103a1384eff298eca7047a1bc20e6a27"
    assert not manifest["independent_roots_adapter_missing_root"]



result = {"source": os.environ["GITHUB_SHA"]}
try:
    inspect(result)
    result["status"] = "PASS"
finally:
    (Path(os.environ["RUNNER_TEMP"]) / "native-launcher.json").write_text(json.dumps(result, indent=2) + "\n")
