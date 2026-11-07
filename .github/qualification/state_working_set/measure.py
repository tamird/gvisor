"""Isolate the two cold-file cohorts during background graph restore."""

import ctypes
from datetime import datetime
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
FILES = ("checkpoint.img", "pages_meta.img", "pages.img")
# Each cell occurs equally often in every position and after each other cell.
CELLS = ("cold", "pages_warm", "state_warm", "warm")
ORDERS = ((0, 1, 3, 2), (1, 2, 0, 3), (2, 3, 1, 0), (3, 0, 2, 1))
GROUPS = 12
EXPECTED = ("091eb2a166fca7ace6a45eddd689d4ac6ca62216ba8a49f7bd4f2f363e14c5de "
            "ab9d6354ba67ba4b3ab9bf7196fa229863dd4ab78938f544ffd2d22d17d09411")
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


def condition_files(directory, cell):
    result = {}
    warm_files = {"cold": (), "pages_warm": FILES[2:], "state_warm": FILES[:2],
                  "warm": FILES}[cell]
    # Condition all files first, then sample all of them immediately before restore.
    for name in FILES:
        with (directory / name).open("rb") as f:
            os.fsync(f.fileno())
            if name in warm_files:
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
            assert resident == (count if name in warm_files else 0), result
    return result


def call(*args, check=True, boot_log=None):
    logging = [] if boot_log is None else [
        "--debug-command=boot", "--debug-log-format=json", "--debug-log=" + str(boot_log)]
    cmd = [str(INSTALL / "runsc"), "--root=" + str(WORK / "runtime"),
           "--platform=systrap", "--network=none", "--overlay2=none",
           "--host-settings=ignore", "--ignore-cgroups", *logging, *map(str, args)]
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


def capture(path):
    ready = ""
    for _ in range(80):
        ready, _, status = call("exec", CID, "/probe", "ready", check=False)
        if status == 0 and re.fullmatch(r"[a-f0-9]{64} [a-f0-9]{64}", ready):
            break
        time.sleep(0.15)
    else:
        raise RuntimeError("guest did not populate both data sets")
    path.mkdir()
    call("checkpoint", "--leave-running", "--compression=none", "--state-engine=graph",
         "--image-path=" + str(path), CID)
    assert media_engine(path / "checkpoint.img") == "graph"
    media = {n: {"bytes": (path / n).stat().st_size, "sha256": digest(path / n)} for n in FILES}
    assert media["pages.img"]["bytes"] >= 512 << 20
    start = time.monotonic_ns()
    verified, end, _ = call("exec", CID, "/probe", "verify")
    assert verified == ready == EXPECTED
    return media, end - start


def duration_ns(value):
    # time.Duration.String() for the millisecond-rounded stock INFO record.
    match = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)(?:\.(\d{1,3}))?s)?(?:(\d+)ms)?", value)
    assert match and value, value
    minutes, seconds, fraction, millis = match.groups()
    return ((int(minutes or 0) * 60 + int(seconds or 0)) * 1000
            + int((fraction or "").ljust(3, "0")) + int(millis or 0)) * 1_000_000


def wall_ns(value):
    clock = re.fullmatch(r"([\d-]+T[\d:]+)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    assert clock
    zone = "+00:00" if clock[3] == "Z" else clock[3]
    seconds = int(datetime.fromisoformat(clock[1] + zone).timestamp())
    return seconds * 1_000_000_000 + int((clock[2] or "").ljust(9, "0"))


def clock_anchor():
    before = time.monotonic_ns()
    wall = time.time_ns()
    after = time.monotonic_ns()
    return {"wall_minus_monotonic_ns": wall - (before + after) // 2,
            "read_span_ns": after - before}


def loader_info(path, start, first, last):
    records = []
    decoder = json.JSONDecoder()
    with path.open() as stream:
        for line in stream:
            if "Async page loading completed in " not in line:
                continue
            # The logger writes JSON and its trailing newline separately. Two
            # goroutines may place whole objects on one line.
            entries = []
            line = line.strip()
            while line:
                entry, end = decoder.raw_decode(line)
                if "Async page loading completed in " in entry["msg"]:
                    entries.append(entry)
                line = line[end:].lstrip()
            assert len(entries) == 1
            entry = entries[0]
            match = re.fullmatch(
                r"(?:save_restore\.go:\d+\] )?Async page loading completed in (\S+) "
                r"\((\d+) bytes, ([\d.]+) MB/s\); (\d+) waiters waited (\S+)~(\S+) for (\d+) bytes",
                entry["msg"])
            assert entry["level"] == "info" and match, "unexpected loader completion"
            elapsed, size, rate, waiters, busy, summed, waited = match.groups()
            stamps = [a["wall_minus_monotonic_ns"] for a in (first, last)]
            records.append({"elapsed_ns": duration_ns(elapsed), "bytes_loaded": int(size),
                "MB_per_second": float(rate), "waiters": int(waiters),
                "any_wait_ns": duration_ns(busy), "summed_wait_ns": duration_ns(summed),
                "waited_bytes": int(waited),
                "done_after_start_ns": wall_ns(entry["time"]) - sum(stamps) // 2 - start})
    assert len(records) == 1, ("loader completions", len(records))
    assert records[0]["bytes_loaded"] >= 512 << 20
    return records[0]


def run():
    env = json.loads((OUT / "build.json").read_text())
    assert env["build_verified"] and env["source"] == SOURCE and platform.machine() == "x86_64"
    assert all(digest(INSTALL / n) == h for n, h in env["release"].items())
    assert digest(BUNDLE / "rootfs/probe") == env["guest"]
    cpus = sorted(os.sched_getaffinity(0))[:4]
    os.sched_setaffinity(0, cpus)
    report = {"source": SOURCE, "cpu_affinity": cpus, "kernel": platform.release(),
              "expected_sha256": EXPECTED.split(), "captures": [], "rows": [], "verified": False,
              "primary_endpoint": "ready_after_return_ns",
              "contrasts": ["pages_warm - cold", "state_warm - cold"]}
    persist(report)
    path = WORK / "graph"
    try:
        for group in range(-1, GROUPS):
            order = [CELLS[i] for i in ORDERS[group % len(ORDERS)]]
            report["at"] = [group, "capture"]
            persist(report)
            call("run", "--detach", "--bundle=" + str(BUNDLE), CID)
            media, original_verify = capture(path)
            report["captures"].append({"group": group, "order": order, "media": media,
                                       "source_verify_ns": original_verify})
            delete()
            for cell in order:
                report["at"] = [group, cell]
                persist(report)
                log_file = WORK / f"boot-{group}-{cell}.jsonl"
                assert not log_file.exists()
                residency = condition_files(path, cell)
                first = clock_anchor()
                start = time.monotonic_ns()
                _, restored, _ = call("restore", "--detach", "--background=true", "--bundle=" + str(BUNDLE),
                                      "--image-path=" + str(path), CID, boot_log=log_file)
                cached, ready, _ = call("exec", CID, "/probe", "ready")
                actual, verified, _ = call("exec", CID, "/probe", "verify")
                assert cached == actual == EXPECTED and start < restored < ready < verified
                row = {"group": group, "cell": cell, "resident_pages": residency,
                       "restore_ns": restored - start, "ready_ns": ready - start,
                       "ready_after_return_ns": ready - restored, "verified_ns": verified - start,
                       "verify_after_ready_ns": verified - ready, "actual_sha256": actual.split()}
                report["rows"].append(row)
                persist(report)
                # Wait only AFTER the application's readback; this never gates READY.
                call("wait", "--restore", CID)
                last = clock_anchor()
                row["clocks"] = [first, last]
                row["loader"] = loader_info(log_file, start, first, last)
                assert max(a["read_span_ns"] for a in (first, last)) < 5_000_000
                assert abs(first["wall_minus_monotonic_ns"] - last["wall_minus_monotonic_ns"]) < 5_000_000
                done = row["loader"]["done_after_start_ns"]
                assert 0 < done < time.monotonic_ns() - start + 5_000_000
                persist(report)
                delete()
                log_file.unlink()
            # This media served all four conditions; check content again outside the timings.
            assert all(digest(path / n) == media[n]["sha256"] for n in FILES)
            shutil.rmtree(path)
        rows = [r for r in report["rows"] if r["group"] >= 0]
        assert len(rows) == len(CELLS) * GROUPS
        assert {(r["group"], r["cell"]) for r in rows} == set(itertools.product(range(GROUPS), CELLS))
        for cell in CELLS:
            lines = []
            for row in rows:
                if row["cell"] != cell:
                    continue
                for name in ("restore", "ready_after_return", "ready", "verified", "verify_after_ready"):
                    lines.append(f"BenchmarkRestore/{name} 1 {row[name+'_ns']} ns/op")
                for name in ("elapsed", "done_after_start", "any_wait", "summed_wait"):
                    lines.append(f"BenchmarkLoader/{name} 1 {row['loader'][name+'_ns']} ns/op")
            (OUT / (cell + ".txt")).write_text("\n".join(lines) + "\n")
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
