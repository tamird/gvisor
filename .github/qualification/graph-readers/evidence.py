"""Verify the staged native binaries and the original per-arm restore measurements."""
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import sys

root = Path(os.environ["RUNNER_TEMP"]) / "graph-readers"
stage = root / "transfer"
out = root / "capsule/work/restore-advice-results"
labels = {"//:release", "//test/rbe/restore_advice:guest", "//test/rbe/restore_advice:inspect"}
modes = ("control", "willneed", "buffer")
members = {"runsc", "containerd-shim-runsc-v1"} | {
    "gvisor-bin/" + name for name in (
        "gvisor_sentry", "checkpointgofer", "runsc-netgofer", "runsc-metric-server",
        "gvisor-sentry-prewarmer", "runsc-fd-parking", "gvisor-proc-umounter",
    )
}

def native_elf(path: Path) -> str:
    assert path.is_file() and not path.is_symlink() and os.access(path, os.X_OK), path.name
    with path.open("rb") as reader:
        header = reader.read(20)
        assert header[:6] == b"\x7fELF\x02\x01" and header[18:20] == b">\x00", path.name
        digest = hashlib.sha256(header)
        for chunk in iter(lambda: reader.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

def build():
    status = {"result": "INCOMPLETE", "source": os.environ["SOURCE"], "labels": sorted(labels)}
    status["prior"] = {"artifact": "13404084616", "sha256": "6db28655c8539b00dbb5e3ea5d4d09ef9d2736140a54901bb37a7221ae7d1078",
                       "restore_exit": 128, "restore_error": 'Rootless mode not supported with "restore"',
                       "restore_stderr_sha256": "50d74823b53d22bdcbaca019e74f60fdc8bf3bf53c6593522c25ba8a14660fc4",
                       "verified_restores": 0, "benchstat_build_exit": 1}
    status["reporter_source"] = os.environ["REPORTER"]
    try:
        for command in ("build", "benchstat"):
            result = stage / (command + ".exit")
            status[command + "_exit"] = int(result.read_text()) if result.exists() else None
            log = stage / (command + ".log")
            if log.exists():
                content = log.read_bytes()
                status[command + "_log_sha256"] = hashlib.sha256(content).hexdigest()
                if status[command + "_exit"] != 0:
                    # The compiler has no credentials. Keep the first Bazel error without
                    # environmental paths, URLs, or command lines.
                    lines = [line for line in content.decode(errors="replace").splitlines()
                             if line.startswith("ERROR: ") and not re.search(r"(?i)://|token|password|credential|authorization|bazelrc|--", line)]
                    if lines:
                        public = re.sub(r"/tmp/[^ :]+|/home/[^ :]+|/src/[^ :]+", "<path>", lines[0])
                        status[command + "_first_error"] = re.sub(r"[^ -~]", "", public)[:360]
                status[command + "_diagnostics"] = [
                    word for word in ("no space left on device", "permission denied",
                    "no such file or directory", "undefined reference", "failed to fetch",
                    "no such package", "repository mapping", "out of memory", "killed")
                    if word.encode() in content.lower()
                ]
        completed = set()
        bep = stage / "build.bep"
        if bep.exists():
            with bep.open() as reader:
                for line in reader:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        status["incomplete_bep"] = True
                        continue
                    target = event["id"].get("targetCompleted", {}).get("label")
                    if target in labels and event.get("completed", {}).get("success"):
                        completed.add(target)
        status["completed"] = sorted(completed)
        assert status["build_exit"] == 0 and completed == labels, status
        installed = stage / "advice-install"
        actual = {str(p.relative_to(installed)) for p in installed.rglob("*") if p.is_file()}
        assert actual == members, actual
        status["elf_sha256"] = {name: native_elf(installed/name) for name in sorted(actual)}
        for name in ("guest", "inspect"):
            status["elf_sha256"][name] = native_elf(stage/name)
        status["result"] = "PASS"
    finally:
        (out/"build.json").write_text(json.dumps(status, indent=2) + "\n")

def finish():
    summary = json.loads((out/"summary.json").read_text())
    built = json.loads((out/"build.json").read_text())
    assert summary["result"] == built["result"] == "PASS"
    assert summary["source"] == built["source"] == os.environ["SOURCE"]
    assert summary["base"] == os.environ["BASE"] and summary["compression"] == "none"
    assert set(summary["graph_frames"]) == {"cpuid", "kernel"}
    assert summary["binaries"] == {"runsc": built["elf_sha256"]["runsc"], "probe": built["elf_sha256"]["guest"]}
    assert not summary["missing"] and not summary["skips"] and not summary["failures"]
    rows = json.loads((out/"samples.json").read_text())
    expected = set(itertools.product(range(18), ("warm", "cold"), modes))
    assert len(rows) == len(expected) == 108
    assert {(r["pair"], r["condition"], r["mode"]) for r in rows} == expected
    for r in rows:
        assert r["bytes_and_pipe_digests_match"] and 0 < r["kernel_load_ms"] < r["restore_cli_ms"]
        assert set(r["residency"]) == {"checkpoint.img", "pages.img", "pages_meta.img"}
        assert all(frac >= .99 if r["condition"] == "warm" else frac <= .01 for frac in r["residency"].values())
    for mode in modes:
        lines = ["BenchmarkRestore/%s 1 %.3f kernel-ms/op %.3f cli-ms/op"
                 % (r["condition"], r["kernel_load_ms"], r["restore_cli_ms"]) for r in rows if r["mode"] == mode]
        assert (out/(mode+".bench")).read_text().splitlines() == lines
    assert built["benchstat_exit"] == 0
    native_elf(stage/"benchstat")
    report = subprocess.check_output([stage/"benchstat", "direct=control.bench", "advice=willneed.bench",
                                      "buffer=buffer.bench"], cwd=out, text=True, timeout=30)
    assert all(token in report for token in ("Restore/cold", "Restore/warm", "kernel-ms/op", "cli-ms/op", "n=18"))
    (out/"benchstat.txt").write_text(report)

def privacy():
    approved = {"build.json", "summary.json", "samples.json", "benchstat.txt", *(mode+".bench" for mode in modes)}
    assert (out/"summary.json").exists()
    for path in out.iterdir():
        assert path.name in approved and path.is_file() and not path.is_symlink()
        assert 0 < path.stat().st_size < 2 << 20
        assert not re.search(rb"(?i)authorization\s*[:=]|x-buildbuddy-api-key|github_pat_|gh[pousr]_[A-Za-z0-9]{20,}", path.read_bytes())

{"build": build, "finish": finish, "privacy": privacy}[sys.argv[1]]()
