"""Time background restore through a real guest memory and pipe readback."""

import ctypes
import hashlib
import itertools
import json
import mmap
import os
from pathlib import Path
import platform
import re
import select
import shutil
import subprocess
import tempfile
import time


SOURCE = "9ee2906f518e1d0f9fd109e9e1feb2812096418b"
WORK = Path(os.environ["RUNNER_TEMP"]) / "state-working-set"
OUT = WORK / "results"
BUNDLE = WORK / "bundle"
INSTALL = WORK / "install"
CID = "state-working-set"
ENGINES = ("wire", "graph")
FILES = ("checkpoint.img", "pages_meta.img", "pages.img")
PAIRS = 16
PAGE = os.sysconf("SC_PAGE_SIZE")
LIBC = ctypes.CDLL(None, use_errno=True)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def media_engine(path):
    # The uncompressed, unsigned statefile starts with the CPUID object.
    with path.open("rb") as f:
        assert f.read(8) == b"gVisorSF"
        length = int.from_bytes(f.read(8), "big")
        assert 0 < length <= 16 << 20
        assert json.loads(f.read(length))["compression"] == "none"
        header = 0
        for shift in range(0, 70, 7):
            byte = f.read(1)[0]
            header |= (byte & 127) << shift
            if byte < 128:
                break
        else:
            raise ValueError("unterminated CPUID header")
        assert header < 1 << 64 and header & (1 << 63)
        return "graph" if header & (1 << 62) else "wire"


def condition_files(directory, warm):
    result = {}
    # Condition all files first, then sample all of them immediately before restore.
    for name in FILES:
        with (directory / name).open("rb") as f:
            os.fsync(f.fileno())
            if warm:
                while f.read(4 << 20):
                    pass
            else:
                os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    for name in FILES:
        with (directory / name).open("rb") as f:
            size = os.fstat(f.fileno()).st_size
            count = (size + PAGE - 1) // PAGE
            vec = (ctypes.c_ubyte * count)()
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_COPY) as view:
                address = ctypes.addressof(ctypes.c_char.from_buffer(view))
                if LIBC.mincore(ctypes.c_void_p(address), ctypes.c_size_t(size), vec) != 0:
                    raise OSError(ctypes.get_errno(), "mincore")
            resident = sum(bit & 1 for bit in vec)
            result[name] = [resident, count]
            assert resident >= count * 0.99 if warm else resident <= count * 0.01, result
    return result


def call(*args, check=True):
    cmd = [str(INSTALL / "runsc"), "--root=" + str(WORK / "runtime"),
           "--platform=systrap", "--network=none", "--overlay2=none",
           "--host-settings=ignore", "--ignore-cgroups", *map(str, args)]
    env = dict(os.environ, GVISOR_SIDECAR_BINARIES_DIR=str(INSTALL / "gvisor-bin"))
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env) as p:
            fd = os.pidfd_open(p.pid)
            try:
                poll = select.poll()
                poll.register(fd, select.POLLIN)
                if not poll.poll(90_000):
                    p.kill()
                    raise TimeoutError("runsc " + str(args[0]))
                status = p.wait()
                ended = time.monotonic_ns()
            finally:
                os.close(fd)
        stdout.seek(0)
        result = stdout.read(65536).decode(errors="replace").strip()
        if check and status:
            stderr.seek(0)
            error = stderr.read(10000).decode(errors="replace").lower()
            classes = [s for s in ("permission denied", "operation not permitted", "no such file",
                       "no space left", "invalid argument", "timed out", "version mismatch",
                       "not found", "not supported", "failed to load", "is not running") if s in error]
            raise RuntimeError((args[0], status, classes, hashlib.sha256(error.encode()).hexdigest()))
        return result, ended, status


def delete():
    call("delete", "--force", CID)
    listed, _, _ = call("list", "--format=json")
    assert not json.loads(listed), "previous sandbox remains"


def persist(report):
    p = OUT / "measurements.json"
    staged = p.with_suffix(".tmp")
    staged.write_text(json.dumps(report, indent=2) + "\n")
    staged.replace(p)


def capture(order):
    ready = ""
    for _ in range(80):
        ready, _, status = call("exec", CID, "/probe", "ready", check=False)
        if status == 0 and re.fullmatch(r"[a-f0-9]{64} [a-f0-9]{64}", ready):
            break
        time.sleep(0.15)
    else:
        raise RuntimeError("guest did not populate both data sets")
    media = {}
    for mode in order:
        d = WORK / mode
        d.mkdir()
        call("checkpoint", "--leave-running", "--compression=none", "--state-engine=" + mode,
             "--image-path=" + str(d), CID)
        assert media_engine(d / "checkpoint.img") == mode
        media[mode] = {n: {"bytes": (d / n).stat().st_size, "sha256": digest(d / n)} for n in FILES}
        assert media[mode]["pages.img"]["bytes"] >= 512 << 20
    # Both images were saved before this source guest first drained its pipes.
    start = time.monotonic_ns()
    verified, end, _ = call("exec", CID, "/probe", "verify")
    assert verified == ready
    return ready, media, end - start


def run():
    env = json.loads((OUT / "build.json").read_text())
    assert env["build_verified"] and env["source"] == SOURCE and platform.machine() == "x86_64"
    assert all(digest(INSTALL / n) == h for n, h in env["release"].items())
    assert digest(BUNDLE / "rootfs/probe") == env["guest"]
    cpus = sorted(os.sched_getaffinity(0))[:4]
    os.sched_setaffinity(0, cpus)
    report = {"source": SOURCE, "cpu_affinity": cpus, "kernel": platform.release(),
              "capture_pairs": [], "rows": [], "verified": False}
    persist(report)
    expected = None
    try:
        for pair in range(-1, PAIRS):
            engines = list(ENGINES if pair % 2 == 0 else reversed(ENGINES))
            report["at"] = [pair, "capture"]
            persist(report)
            call("run", "--detach", "--bundle=" + str(BUNDLE), CID)
            ready, media, original_verify = capture(engines)
            assert expected is None or ready == expected
            expected = ready
            report["expected_sha256"] = ready.split()
            report["capture_pairs"].append({"pair": pair, "order": engines, "media": media,
                                            "source_verify_ns": original_verify})
            delete()
            conditions = ("warm", "cold") if pair % 4 in (0, 3) else ("cold", "warm")
            for condition in conditions:
                # Independent of capture order and of early/late cache condition.
                order = ENGINES if (pair % 4 < 2) == (condition == "warm") else reversed(ENGINES)
                for mode in order:
                    report["at"] = [pair, condition, mode]
                    persist(report)
                    path = WORK / mode
                    residency = condition_files(path, condition == "warm")
                    start = time.monotonic_ns()
                    _, restored, _ = call("restore", "--detach", "--background=true",
                                          "--bundle=" + str(BUNDLE), "--image-path=" + str(path), CID)
                    cached_answer, accepted, _ = call("exec", CID, "/probe", "ready")
                    actual_answer, verified, _ = call("exec", CID, "/probe", "verify")
                    assert cached_answer == actual_answer == expected
                    assert start < restored < accepted < verified
                    report["rows"].append({"pair": pair, "condition": condition, "engine": mode,
                        "resident_pages": residency, "restore_ns": restored - start,
                        "ready_ns": accepted - start, "verified_ns": verified - start,
                        "verify_after_ready_ns": verified - accepted,
                        "actual_sha256": actual_answer.split()})
                    persist(report)
                    delete()
            for mode in ENGINES:
                shutil.rmtree(WORK / mode)
        rows = [r for r in report["rows"] if r["pair"] >= 0]
        assert len(rows) == 4 * PAIRS
        assert {(r["pair"], r["condition"], r["engine"]) for r in rows} == set(
            itertools.product(range(PAIRS), ("warm", "cold"), ENGINES))
        for engine in ENGINES:
            bench = []
            for row in rows:
                if row["engine"] == engine:
                    for name in ("restore", "ready", "verified", "verify_after_ready"):
                        bench.append(f"BenchmarkRestore/{row['condition']}/{name} 1 {row[name+'_ns']} ns/op")
            (OUT / (engine + ".txt")).write_text("\n".join(bench) + "\n")
        report["verified"] = True
        report.pop("at", None)
    except Exception as exc:
        report["failure"] = type(exc).__name__ + " " + str(exc)[:750]
        raise
    finally:
        persist(report)
        call("delete", "--force", CID, check=False)


if __name__ == "__main__":
    run()
