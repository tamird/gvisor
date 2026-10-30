"""Record the qualified PackageTar actions and inspect the bytes each compressor produced."""

import bz2
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

from original_cas import fields, get, ident, key, one
from path_comparison import digest

TEMP = Path(os.environ["RUNNER_TEMP"])
BUILTINS = {"payload": "tar", "payload_gzip": "gz", "payload_bzip2": "bz2", "payload_xz": "xz"}
DECLARED = {"path_pigz": "gz", "path_zstd": "zstd"}


def verify_writer(spawn, name, compression, row):
    row.update(action=digest(spawn["digest"]), runner=spawn.get("runner"), hit=spawn.get("cacheHit", False),
               exit=spawn.get("exitCode", 0), start=spawn.get("metrics", {}).get("startTime"))
    row["platform"] = {p["name"]: p["value"] for p in spawn["platform"]["properties"]}
    action = get(row["action"])
    row["command"] = ident(one(action, 1))
    row["input_root"] = ident(one(action, 2))
    command = get(row["command"])
    row["path"] = [one(v, 2).decode() for n, v in fields(command) if n == 2 and one(v, 1) == b"PATH"]
    argv = [v.decode() for n, v in fields(command) if n == 1]
    row["compressor"] = [arg.removeprefix("--compressor=") for arg in argv if arg.startswith("--compressor=")]
    if "--compressor" in argv:
        row["compressor"].append(argv[argv.index("--compressor") + 1])
    if bool(row["compressor"]) != (name in DECLARED):
        raise ValueError("Unexpected compressor argument")
    if (row["path"] or row["exit"] or row["platform"].get("Arch") != "amd64"
            or row["runner"] not in ("remote", "remote cache hit")):
        raise ValueError("Unexpected remote PATH, executor or exit")
    [archive] = spawn["actualOutputs"]
    row["archive"] = digest(archive["digest"])
    content = get(row["archive"])
    if compression == "zstd":
        tool = shutil.which("zstd")
        if not tool:
            raise ValueError("Cannot inspect zstd archive: coordinator zstd unavailable")
        content = subprocess.check_output([tool, "-dc"], input=content)
    elif compression == "gz":
        content = gzip.decompress(content)
    elif compression == "bz2":
        content = bz2.decompress(content)
    elif compression == "xz":
        content = lzma.decompress(content)
    row["tar"] = hashlib.sha256(content).hexdigest()
    if name in BUILTINS and row["tar"] != "3a57a5fe4121f867ccd0c26c89c511dd0f5851712ae6723babed02f950193898":
        raise ValueError("Changed qualified tar")
    with tarfile.open(fileobj=io.BytesIO(content)) as archive:
        [member] = [entry for entry in archive if entry.isfile()]
        row["member"] = member.name
        reference = Path("debian/description" if name in DECLARED else "test/rbe/python_package_writers/notice.txt")
        if archive.extractfile(member).read() != reference.read_bytes():
            raise ValueError(f"Unexpected archive member {member.name}")


def observe(group, arm, result):
    directory = TEMP / f"{group}-{arm}"
    labels = BUILTINS if group == "builtin" else DECLARED
    expected = {name: {"status": "missing"} for name in labels}
    result["targets"] = expected
    for field in ("started", "finished", "qualification-exit"):
        path = directory / field
        if path.exists():
            result[field] = path.read_text().strip()
    evidence = directory / "qualification"
    [native] = evidence.rglob("native-exit.txt")
    result["native_exit"] = int(native.read_text())
    [events] = evidence.rglob("events.jsonl")
    for line in events.read_text().splitlines():
        e = json.loads(line)
        if "started" in e:
            result["invocation"] = e["started"]["uuid"]
            result["bazel_started"] = e["started"].get("startTime")
        if "finished" in e:
            result["bazel_finished"] = e["finished"].get("finishTime")
    result["errors"] = []
    for path in evidence.rglob("stderr.log"):
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("ERROR:") and (not key or key not in line):
                result["errors"].append(line[:700])
    [log] = evidence.rglob("execution.binpb")
    text = subprocess.check_output([TEMP / "bb", "print", "--compact_execution_log=" + str(log)], text=True)
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        spawn, pos = decoder.raw_decode(text, pos)
        name = spawn.get("targetLabel", "").rsplit(":", 1)[-1]
        if spawn.get("mnemonic") != "PackageTar" or name not in labels:
            continue
        row = expected[name]
        if row["status"] != "missing":
            raise ValueError(f"Duplicate {name}")
        try:
            verify_writer(spawn, name, labels[name], row)
            row["status"] = "ok"
        except Exception as error:
            row["status"] = f"{type(error).__name__}: {error}"


def main():
    report = {"source": os.environ["GITHUB_SHA"], "stock": "https://github.com/tamird/gvisor/actions/runs/40995230460",
              "arms": {}, "comparisons": {}}
    for group, labels in (("builtin", BUILTINS), ("declared", DECLARED)):
        for arm in ("a", "b"):
            row = report["arms"][f"{group}-{arm}"] = {}
            try:
                observe(group, arm, row)
            except Exception as error:
                row["error"] = f"{type(error).__name__}: {error}"
        a = report["arms"][group + "-a"]
        b = report["arms"][group + "-b"]
        for name in labels:
            first = a.get("targets", {}).get(name, {})
            second = b.get("targets", {}).get(name, {})
            fields_to_compare = ("action", "command", "input_root", "archive", "tar")
            report["comparisons"][name] = (
                first.get("status") == second.get("status") == "ok"
                and all(first[k] == second[k] for k in fields_to_compare)
            )
    report["status"] = "PASS" if all(report["comparisons"].values()) and all(
        r.get("native_exit") == 0 and r.get("qualification-exit") == "0" for r in report["arms"].values()
    ) else "INCOMPLETE"
    (TEMP / "package-tar-compression.json").write_text(json.dumps(report, indent=2) + "\n")
    print(report["status"], report["comparisons"])
    return report["status"] != "PASS"


if __name__ == "__main__":
    raise SystemExit(main())
