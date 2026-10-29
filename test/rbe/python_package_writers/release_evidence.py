"""Validate the native writer actions and the installable release bytes."""

import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tarfile


def validate(result):
    folder = Path(os.environ["RUNNER_TEMP"])
    arch = result["arch"]
    with tarfile.open(folder / "qualification.tar.gz") as archive:
        def read(suffix):
            path, = (k for k in archive.getnames() if k.endswith(suffix))
            return archive.extractfile(path).read()
        assert archive.extractfile("final-head.txt").read().decode().strip() == result["source"]
        result["execution_finished"] = read("/execution-finished.txt").decode().strip()
        events = [json.loads(s) for s in read("/events.jsonl").splitlines()]
        result["invocation"], = (r["started"]["uuid"] for r in events if "started" in r)
        result["exit"] = int(read("/native-exit.txt"))
        record = read("/execution.binpb")
    result["record_sha256"] = hashlib.sha256(record).hexdigest()
    log = folder / "original.binpb"
    log.write_bytes(record)
    printed = subprocess.check_output([os.environ["BB_LOG_DECODER"], "print", "--compact_execution_log=" + str(log)], text=True)
    execroot = Path("bazel-out").resolve().parent
    targets = {("MakeDeb", "//debian:debian"), ("PackageTar", "//debian:gvisor-release-tar-bz2"), ("PackageTar", "//shim:config")}
    pos = 0
    parser = json.JSONDecoder()
    while pos < len(printed):
        if printed[pos].isspace():
            pos += 1
            continue
        row, pos = parser.raw_decode(printed, pos)
        label = row.get("targetLabel", "").removeprefix("@@")
        key = (row.get("mnemonic"), label)
        if key not in targets:
            continue
        props = {p["name"]: p["value"] for p in row["platform"]["properties"]}
        rec = result["writers"][label] = {"digest": row["digest"], "runner": row["runner"], "cache": row.get("cacheHit", False), "cpu": props["Arch"], "image": props["container-image"], "exit": row.get("exitCode", 0), "program": row["commandArgs"][0]}
        assert rec["cpu"] == arch and rec["runner"] == "remote" and not rec["cache"] and "hello-world@sha256:" in rec["image"], rec
        assert "python" in rec["program"] and any("stage2_bootstrap" in arg for arg in row["commandArgs"]), rec
        if not rec["exit"]:
            for o in row["actualOutputs"]:
                if o["path"].endswith((".deb", ".bz2", "config.tar")):
                    f = execroot / o["path"]
                    if f.is_file():
                        with f.open("rb") as stream:
                            sha = hashlib.file_digest(stream, "sha256").hexdigest()
                        assert sha == o["digest"]["hash"]
                        rec["output"] = {"path": str(f), "sha256": sha, "size": f.stat().st_size}
    assert result["exit"] == 0 and set(result["writers"]) == {k[1] for k in targets}, result
    def path(label):
        return result["writers"][label]["output"]["path"]
    deb = path("//debian:debian")
    tar = path("//debian:gvisor-release-tar-bz2")
    for k,v in {"Package":"runsc", "Architecture": arch}.items():
        assert subprocess.check_output(["dpkg-deb", "--field", deb, k], text=True).strip()==v
    result["version"] = subprocess.check_output(["dpkg-deb", "--field", deb, "Version"], text=True).strip()
    assert result["version"]
    with tarfile.open(fileobj=io.BytesIO(subprocess.check_output(["dpkg-deb","--fsys-tarfile",deb]))) as installed, tarfile.open(tar) as release:
        def content(archive, filename):
            member, = (m for m in archive if m.name.removeprefix("./").lstrip("/") == filename)
            return archive.extractfile(member).read()
        for name in ("runsc","containerd-shim-runsc-v1"):
            binary = content(installed, "usr/bin/" + name)
            assert binary == content(release,name)
            assert binary[:5] == b"\x7fELF\x02" and struct.unpack_from("<H", binary, 18)[0] == {"amd64":62,"arm64":183}[arch]
            result["binary_hashes"][name] = hashlib.sha256(binary).hexdigest()
        assert content(installed,"etc/containerd/runsc.toml")==Path("shim/runsc.toml").read_bytes()

result = {"source":os.environ["GITHUB_SHA"],"arch":os.environ["QUALIFICATION_ARCH"],"expected":["//debian:debian","//debian:gvisor-release-tar-bz2","//shim:config"],"writers":{},"binary_hashes":{},"status":"FAIL"}
try:
    validate(result)
    result["status"]="PASS"
finally:
    result["verified"]=datetime.datetime.now(datetime.timezone.utc).isoformat()
    (Path(os.environ["REPORT_DIR"])/(result["arch"]+".json")).write_text(json.dumps(result,indent=2)+"\n")
