"""Check the original worker records and compare complete packages to the controls."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tarfile


def check(result):
    folder = Path(os.environ["RUNNER_TEMP"])
    expected = {
        "PackageTar": ("payload.tar", "3a57a5fe4121f867ccd0c26c89c511dd0f5851712ae6723babed02f950193898"),
        "MakeDeb": ("gvisor-writer-input_1_all.deb", "486d93a4ea624b12536af762811caeac49ab801c05169c7dc744d737673ab015"),
    }
    result["expected"] = expected
    with tarfile.open(folder / "qualification.tar.gz") as archive:
        def read(suffix):
            paths = [k for k in archive.getnames() if k.endswith(suffix)]
            assert len(paths) == 1, (suffix, paths)
            return archive.extractfile(paths[0]).read()
        assert archive.extractfile("final-head.txt").read().decode().strip() == result["source"]
        result["execution_finished"] = read("/execution-finished.txt").decode().strip()
        result["exit"] = int(read("/native-exit.txt"))
        result["invocation"], = (r["started"]["uuid"] for r in map(json.loads, read("/events.jsonl").splitlines()) if "started" in r)
        record = read("/execution.binpb")
    result["record_sha256"] = hashlib.sha256(record).hexdigest()
    log = folder / "native.binpb"
    log.write_bytes(record)
    printed = subprocess.check_output([os.environ["BB_LOG_DECODER"], "print", "--compact_execution_log=" + str(log)], text=True)
    execroot = Path("bazel-out").resolve().parent
    while printed.strip():
        printed = printed.lstrip()
        row, pos = json.JSONDecoder().raw_decode(printed)
        printed = printed[pos:]
        name = row.get("mnemonic")
        if name not in expected:
            continue
        assert name not in result["workers"], name
        props = {p["name"]: p["value"] for p in row["platform"]["properties"]}
        rec = result["workers"][name] = {"action": row["digest"], "platform": props, "runner": row["runner"], "cache_hit": row.get("cacheHit", False), "exit": row.get("exitCode", 0), "argv": row["commandArgs"][:2]}
        assert rec["runner"] == "remote" and not rec["cache_hit"] and props["Arch"] == result["arch"] and "hello-world@sha256:" in props["container-image"], rec
        assert "python" in rec["argv"][0] and any("stage2_bootstrap" in s for s in row["commandArgs"]), row["commandArgs"]
        exe = (execroot / rec["argv"][0]).read_bytes()
        assert exe[:5] == b"\x7fELF\x02" and int.from_bytes(exe[18:20], "little") == {"amd64": 62, "arm64": 183}[result["arch"]]
        off = struct.unpack_from("<Q", exe, 32)[0]
        size, count = struct.unpack_from("<HH", exe, 54)
        assert all(struct.unpack_from("<I", exe, off + i * size)[0] != 3 for i in range(count))
        rec["interpreter"] = hashlib.sha256(exe).hexdigest()
        assert not rec["exit"], rec
        filename, sha = expected[name]
        package, = (o for o in row["actualOutputs"] if o["path"].endswith("/" + filename))
        content = (execroot / package["path"]).read_bytes()
        rec["output"] = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        assert rec["output"]["sha256"] == sha == package["digest"]["hash"], rec
    assert result["exit"] == 0 and result["workers"].keys() == expected.keys(), result


result = {"source": os.environ["GITHUB_SHA"], "arch": os.environ["QUALIFICATION_ARCH"], "workers": {}, "status": "FAIL"}
try:
    check(result)
    result["status"] = "PASS"
finally:
    result["verified"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path = Path(os.environ["REPORT_DIR"]) / (result["arch"] + ".json")
    path.write_text(json.dumps(result, indent=2) + "\n")
