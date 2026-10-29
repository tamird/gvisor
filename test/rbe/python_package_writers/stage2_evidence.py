"""Check the original worker records and compare complete packages to the controls."""

import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tarfile
import tempfile


def check(result):
    folder = Path(os.environ["RUNNER_TEMP"])
    expected = {"PackageVersion": ("version.txt", "package_version"), "Changelog": ("changelog", "changelog_tool")}
    label = os.environ["RELEASE_LABEL"]
    version, date = {
        "release-20261029.0": ("20261029.0", "Thu, 29 Oct 2026 00:00:00 +0000"),
        "git-9c271665f": ("9c271665f", "Thu, 01 Jan 1970 00:00:00 +0000"),
    }[label]
    result["label"] = label
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
        if any(o["path"].endswith("/bin/debian/changelog") for o in row.get("actualOutputs", [])):
            name = "Changelog"
        if name == "PackageTar" and any(o["path"].endswith("/config.tar") for o in row.get("actualOutputs", [])):
            name = "ShimConfig"
        if name not in expected:
            continue
        assert name not in result["workers"], name
        props = {p["name"]: p["value"] for p in row["platform"]["properties"]}
        rec = result["workers"][name] = {"action": row["digest"], "platform": props, "runner": row["runner"], "cache_hit": row.get("cacheHit", False), "exit": row.get("exitCode", 0), "argv": row["commandArgs"][:2]}
        assert rec["runner"] == "remote" and not rec["cache_hit"] and props["Arch"] == result["arch"], rec
        assert props["container-image"] == "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef", rec
        assert rec["argv"][0].endswith("/" + expected[name][1]), rec
        assert len(row["commandArgs"]) == (2 if name == "PackageVersion" else 3), rec
        if name == "Changelog":
            result["version_consumed"] = rec["argv"][1]
        exe = (execroot / rec["argv"][0]).read_bytes()
        assert exe[:5] == b"\x7fELF\x02" and int.from_bytes(exe[18:20], "little") == {"amd64": 62, "arm64": 183}[result["arch"]]
        off = struct.unpack_from("<Q", exe, 32)[0]
        size, count = struct.unpack_from("<HH", exe, 54)
        has_loader = any(struct.unpack_from("<I", exe, off + i * size)[0] == 3 for i in range(count))
        assert not has_loader, rec
        rec["tool_sha256"] = hashlib.sha256(exe).hexdigest()
        rec["dynamic_loader"] = has_loader
        assert not rec["exit"], rec
        filename, _ = expected[name]
        package, = (o for o in row["actualOutputs"] if o["path"].endswith("/" + filename))
        content = (execroot / package["path"]).read_bytes()
        rec["output"] = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        assert rec["output"]["sha256"] == package["digest"]["hash"], rec
        assert row["commandArgs"][-1] == package["path"], rec
        if name == "PackageVersion":
            assert content == (version + "\n").encode(), rec
            result["version_produced"] = package["path"]
        else:
            assert content == (f"runsc ({version}) unstable; urgency=medium\n\n  * See https://gvisor.dev/docs/ for release details.\n\n -- The gVisor Authors <gvisor-dev@googlegroups.com>  {date}\n").encode(), rec
    assert result["version_produced"] == result["version_consumed"], result
    assert result["exit"] == 0 and result["workers"].keys() == expected.keys(), result


result = {"source": os.environ["GITHUB_SHA"], "arch": os.environ["QUALIFICATION_ARCH"], "workers": {}, "status": "FAIL"}
try:
    check(result)
    result["status"] = "PASS"
except Exception as err:
    result["error"] = str(err)
    raise
finally:
    result["missing"] = sorted(set(result.get("expected", {})) - set(result["workers"]))
    result["verified"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path = Path(os.environ["REPORT_DIR"]) / ("worker-" + result["arch"] + "-" + os.environ["RELEASE_LABEL"] + ".json")
    path.write_text(json.dumps(result, indent=2) + "\n")
