"""Measure a four-worker Nginx service with both shipping state engines at three CPU policies."""

import ctypes
from collections import Counter
import hashlib
import io
import itertools
import json
import mmap
import os
from pathlib import Path
import platform
import re
import select
import shutil
import statistics
import subprocess
import tempfile
import time
import zlib
import sys


SOURCE = "451b92800b9cb82262dc8a735388f7cf4d17d895"
WORK = Path(os.environ["RUNNER_TEMP"]) / "state-working-set"
OUT = WORK / "results"
BUNDLE = WORK / "bundle"
INSTALL = WORK / "install"
CID = "state-nginx"
ENGINES = ("wire", "graph")
FILES = ("checkpoint.img", "pages_meta.img", "pages.img")
POLICIES = ("unlimited", "oci-quarter", "ancestor-quarter")
CELLS = tuple(itertools.product(POLICIES, ENGINES))
# Rotating this six-period Williams row balances positions and carryover.
ORDER = (0, 1, 5, 2, 4, 3)
CG = Path("/sys/fs/cgroup")
GROUP = CG / "workload"
LEAF = GROUP / "sandbox"
QUARTER = "25000 100000"
UNLIMITED = "max 100000"
BOOST = "100000 100000"
METRICS = ("restore_command", "through_first_http_response", "restore_group_cpu")
PAIRS = 12
PAGE = os.sysconf("SC_PAGE_SIZE")
LIBC = ctypes.CDLL(None, use_errno=True)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def media_engine(path, compression):
    # Both unsigned statefiles start with the CPUID object after decompression.
    with path.open("rb") as f:
        assert f.read(8) == b"gVisorSF"
        length = int.from_bytes(f.read(8), "big")
        assert 0 < length <= 16 << 20
        assert json.loads(f.read(length))["compression"] == compression
        stream = f
        if compression == "flate-best-speed":
            chunk_size = int.from_bytes(f.read(4), "big")
            compressed_size = int.from_bytes(f.read(4), "big")
            assert chunk_size == 1 << 20 and 0 < compressed_size < 2 * chunk_size
            decoder = zlib.decompressobj(-zlib.MAX_WBITS)
            prefix = decoder.decompress(f.read(compressed_size), chunk_size + 1)
            assert decoder.eof and not decoder.unused_data and 0 < len(prefix) <= chunk_size
            stream = io.BytesIO(prefix)
        header = 0
        for shift in range(0, 70, 7):
            byte = stream.read(1)[0]
            header |= (byte & 127) << shift
            if byte < 128:
                break
        else:
            raise ValueError("unterminated CPUID header")
        assert header < 1 << 64 and header & (1 << 63)
        return "graph" if header & (1 << 62) else "wire"


def warm_files(directory, names):
    result = {}
    # Condition all files first, then sample all of them immediately before restore.
    for name in names:
        with (directory / name).open("rb") as f:
            os.fsync(f.fileno())
            while f.read(4 << 20):
                pass
    for name in names:
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
            assert resident >= count * 0.99, result
    return result


def call(*args, check=True, observe=None):
    cmd = [str(INSTALL / "runsc"), "--root=" + str(WORK / "runtime"),
           "--platform=systrap", "--network=none", "--overlay2=none",
           "--host-settings=ignore", *map(str, args)]
    env = dict(os.environ, GVISOR_SIDECAR_BINARIES_DIR=str(INSTALL / "gvisor-bin"))
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env) as p:
            fd = os.pidfd_open(p.pid)
            try:
                poll = select.poll()
                poll.register(fd, select.POLLIN)
                deadline = time.monotonic() + 90
                while not poll.poll(5 if observe else 90_000):
                    if time.monotonic() >= deadline:
                        p.kill()
                        raise TimeoutError("runsc " + str(args[0]))
                    if observe:
                        observe()
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
            # Workload stderr belongs to this isolated, credential-free runsc process.
            lines = [line[:240] for line in error.splitlines() if line.strip()]
            raise RuntimeError((args[0], status, classes, lines[-2:]))
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


def read_http(check=True):
    body, ended, status = call("exec", CID, "/usr/bin/curl", "--silent", "--show-error",
                               "--fail", "--max-time", "4", "--noproxy", "*", "--write-out",
                               "%{http_code}", "http://127.0.0.1/latin1k.txt", check=check)
    if status:
        return None, ended
    assert body == "\x00" * 1024 + "200", (len(body), body[-3:])
    return hashlib.sha256(body[:-3].encode()).hexdigest(), ended


def ready_guest():
    for _ in range(80):
        digest, _ = read_http(check=False)
        if digest is not None:
            return digest
        time.sleep(0.15)
    raise RuntimeError("nginx did not serve the benchmark file")


def nginx_pids():
    pids = sorted(json.loads(call("ps", "--format=json", CID)[0]))
    assert len(pids) == 5 and pids[0] == 1, pids
    return pids


def wait_quiescent():
    # checkpointfile/checkpointfile.go takes four 500 ms samples below 0.5% CPU.
    quiet = 0
    deadline = time.monotonic() + 60
    while quiet < 4:
        start = time.monotonic_ns()
        before = stats(LEAF)["usage_usec"]
        time.sleep(0.5)
        used_ns = 1000 * (stats(LEAF)["usage_usec"] - before)
        quiet = quiet + 1 if used_ns < (time.monotonic_ns() - start) * 0.005 else 0
        if time.monotonic() >= deadline:
            raise RuntimeError("nginx did not quiesce")


def capture(cell, path, source_pid):
    compression, engine = cell
    path.mkdir()
    limits = []
    def observe():
        limit = quota(LEAF)
        if not limits or limits[-1][1] != limit:
            assert membership(source_pid) == "/workload/sandbox"
            limits.append([time.monotonic_ns() - start, limit])
    before = stats(LEAF)
    start = time.monotonic_ns()
    observe()
    _, ended, _ = call("checkpoint", "--leave-running", "--compression=" + compression,
                       "--state-engine=" + engine, "--image-path=" + str(path), CID, observe=observe)
    observe()
    after = stats(LEAF)
    names = FILES if compression == "none" else FILES[:1]
    assert {p.name for p in path.iterdir()} == set(names), sorted(p.name for p in path.iterdir())
    assert media_engine(path / "checkpoint.img", compression) == engine
    files = {name: {"bytes": (path / name).stat().st_size, "sha256": digest(path / name)} for name in names}
    total = sum(f["bytes"] for f in files.values())
    assert all(f["bytes"] > 0 for f in files.values()), files
    return {"save_command_ns": ended - start, "media_bytes": total, "files": files,
            "observed_leaf_limits": limits, "cpu_stat_delta": {k: after[k]-v for k, v in before.items()}}


def membership(pid):
    lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    return next(s[3:] for s in lines if s.startswith("0::"))


def host_caps(stage):
    parent = CG / membership("self").lstrip("/")
    assert parent.name == os.environ["UNIT"] + ".service", parent.name
    caps = []
    for group in (parent, *parent.parents):
        if group == CG:
            break
        value = (group / "cpu.max").read_text().strip()
        units, period = value.split()
        assert units == "max" or int(units) >= 4*int(period), value
        caps.append(value)
    owned = parent.stat()
    record = {"ancestor_cpu_max": caps, "inode": owned.st_ino, "device": owned.st_dev}
    (OUT / ("ancestors-" + stage + ".json")).write_text(json.dumps(record))
    print(parent)


def prepare_groups(stage):
    # Identify the delegated unit before writing any controller files.
    owned = json.loads((OUT / ("ancestors-" + stage + ".json")).read_text())
    actual = CG.stat()
    assert [actual.st_dev, actual.st_ino] == [owned["device"], owned["inode"]], owned
    assert (CG / "cgroup.procs").read_text().split() == [str(os.getpid())], "delegated service has other members"
    assert membership("self") == "/", membership("self")
    controllers = (CG / "cgroup.controllers").read_text().split()
    assert {"cpu", "cpuset", "io", "memory", "pids"} <= set(controllers), controllers
    observer = CG / "observer"
    observer.mkdir()
    (observer / "cgroup.procs").write_text(str(os.getpid()))
    assert not (CG / "cgroup.procs").read_text().strip()
    enable = " ".join("+" + key for key in controllers)
    (CG / "cgroup.subtree_control").write_text(enable)
    GROUP.mkdir()
    (GROUP / "cgroup.subtree_control").write_text(enable)
    assert membership("self") == "/observer"
    return controllers


def stats(group):
    entries = dict(row.split() for row in (group / "cpu.stat").read_text().splitlines())
    return {key: int(entries[key]) for key in ("usage_usec", "nr_periods", "nr_throttled", "throttled_usec")}


def quota(group):
    return (group / "cpu.max").read_text().strip()


def bind_sentry(config):
    info = json.loads(call("state", CID)[0])
    assert info["id"] == CID and info["status"] == "running", info.get("status")
    pid = info["pid"]
    exe = Path(f"/proc/{pid}/exe")
    assert pid > 1 and membership(pid) == "/workload/sandbox", (pid, membership(pid))
    assert exe.samefile(INSTALL / "gvisor-bin/gvisor_sentry"), (pid, os.readlink(exe))
    assert quota(LEAF) == (UNLIMITED if config == "unlimited" else QUARTER), quota(LEAF)
    return {"pid": pid, "group": membership(pid), "application_cpu_max": quota(LEAF), "exe_inode": exe.stat().st_ino}


def set_spec(config, template):
    resources = {} if config == "unlimited" else {"cpu": {"quota": 25000, "period": 100000}}
    template["linux"].update(cgroupsPath="/workload/sandbox", resources=resources)
    (BUNDLE / "config.json").write_text(json.dumps(template))


def capture_both(config, engines, template, expected, row):
    assert not LEAF.exists(), "previous runsc leaf is still present"
    (GROUP / "cpu.max").write_text(UNLIMITED)
    set_spec(config, template)
    call("run", "--detach", "--bundle=" + str(BUNDLE), CID)
    ready = ready_guest()
    assert expected is None or ready == expected
    row["source_guest"] = bind_sentry(config)
    row["parent_cpu_max"] = quota(GROUP)
    row["response_sha256"] = ready
    row["pids"] = nginx_pids()
    for engine in engines:
        wait_quiescent()
        row[engine] = capture(("none", engine), WORK / (config + "-" + engine), row["source_guest"]["pid"])
    assert read_http()[0] == ready
    delete()
    return ready


def restore(policy, engine, path, expected, expected_pids, row):
    config = "unlimited" if policy == "unlimited" else "oci-quarter"
    assert not LEAF.exists(), "runsc must create a fresh leaf"
    cap = QUARTER if policy == "ancestor-quarter" else UNLIMITED
    (GROUP / "cpu.max").write_text(cap)
    assert quota(GROUP) == cap
    row.update(policy=policy, engine=engine, parent_cpu_max=cap, resident=warm_files(path, FILES))
    observed = row["observed"] = []
    inode = (INSTALL / "gvisor-bin/gvisor_sentry").stat()
    known_pid = None

    def sample():
        nonlocal known_pid
        try:
            limit = quota(LEAF)
            if known_pid is None:
                for token in (LEAF / "cgroup.procs").read_text().split():
                    try:
                        st = os.stat("/proc/" + token + "/exe")
                    except (FileNotFoundError, ProcessLookupError, PermissionError):
                        continue
                    if (st.st_dev, st.st_ino) == (inode.st_dev, inode.st_ino):
                        known_pid = int(token)
                        break
            key = [limit, known_pid]
            if not observed or key != observed[-1][1:]:
                observed.append([time.monotonic_ns() - start, *key])
        except FileNotFoundError:
            pass  # runsc creates the leaf during restore.

    charge = row["cpu_stat"] = {"before": stats(GROUP)}
    start = time.monotonic_ns()
    _, restored, _ = call("restore", "--detach", "--background=false", "--bundle=" + str(BUNDLE),
                          "--image-path=" + str(path), CID, observe=sample)
    charge["returned"] = stats(GROUP)
    # The leaf is newly created by this restore; its counters started at zero.
    leaf = row["fresh_leaf_cpu_stat"] = {"returned": stats(LEAF)}
    actual, verified = read_http()
    charge["verified"] = stats(GROUP)
    leaf["verified"] = stats(LEAF)
    assert actual == expected and start < restored < verified
    row["response_sha256"] = actual
    row["pids"] = nginx_pids()
    assert row["pids"] == expected_pids, (row["pids"], expected_pids)
    row["sentry"] = identity = bind_sentry(config)
    assert known_pid is None or identity["pid"] == known_pid, (identity, known_pid)
    assert quota(GROUP) == cap
    row["sentry_observed_during_restore"] = known_pid is not None
    row["boost_observed_on_sentry"] = any(v == BOOST and pid == identity["pid"] for _, v, pid in observed)
    for key in charge["before"]:
        assert charge["before"][key] <= charge["returned"][key] <= charge["verified"][key]
    assert leaf["returned"]["usage_usec"] > 0
    for key in leaf["returned"]:
        assert leaf["returned"][key] <= leaf["verified"][key]
    # A small service can finish within its current quota period. Preserve
    # the throttle counters even when the correctly applied limit never binds.
    row["metrics_ns"] = dict(restore_command=restored-start, through_first_http_response=verified-start,
                             restore_group_cpu=1000*(charge["returned"]["usage_usec"]-charge["before"]["usage_usec"]))
    assert all(value > 0 for value in row["metrics_ns"].values())


def write_bench(blocks):
    measured = [block for block in blocks if block["pair"] >= 0]
    for config in ("unlimited", "oci-quarter"):
        # A second checkpoint can benefit from work done by the first. Check
        # the order actually captured on each live guest, at both quotas.
        orders = Counter()
        for block in measured:
            sources = block["sources"]
            first = next(engine for engine in sources[config] if engine in ENGINES)
            orders[tuple(sources).index(config), first] += 1
        expected = {(position, engine): PAIRS // 4 for position in (0, 1) for engine in ENGINES}
        assert orders == expected, (config, orders, expected)
    for engine in ENGINES:
        lines = []
        for block in measured:
            for config, source in block["sources"].items():
                capture = source[engine]
                elapsed = capture["save_command_ns"]
                leaf_cpu = capture["cpu_stat_delta"]["usage_usec"] * 1000
                assert elapsed > 0 and leaf_cpu > 0, (config, engine, capture)
                lines.append(f"BenchmarkCheckpoint/{config}/command 1 {elapsed} ns/op")
                lines.append(f"BenchmarkCheckpoint/{config}/sandbox_cpu 1 {leaf_cpu} ns/op")
            for row in block["cells"]:
                if row["engine"] == engine:
                    for name, value in row["metrics_ns"].items():
                        lines.append(f"BenchmarkRestore/{row['policy']}/{name} 1 {value} ns/op")
        assert len(lines) == PAIRS * (4 + len(POLICIES) * len(METRICS)), len(lines)
        (OUT / (engine + ".txt")).write_text("\n".join(lines) + "\n")
    for block in blocks:
        samples = {(r["policy"], r["engine"]): r["metrics_ns"] for r in block["cells"]}
        ratios = block["graph_over_wire"] = {
            p: {m: samples[p, "graph"][m] / samples[p, "wire"][m] for m in METRICS} for p in POLICIES}
        block["quarter_vs_unlimited_engine_ratio"] = {
            m: ratios["oci-quarter"][m] / ratios["unlimited"][m] for m in METRICS}


def run():
    env = json.loads((OUT / "build.json").read_text())
    assert env["build_verified"] and env["source"] == SOURCE and platform.machine() == "x86_64"
    assert all(digest(INSTALL / n) == h for n, h in env["release"].items())
    assert env["image_config"] == "sha256:5f9e05caa37420ad50b7ccf873ea117fcbd8cff24b3fcb689cb904bcab624cf5"
    cpus = sorted(os.sched_getaffinity(0))[:4]
    assert len(cpus) == 4, cpus
    os.sched_setaffinity(0, cpus)
    report = {"source": SOURCE, "image_config": env["image_config"],
              "image_manifest": "sha256:31c649d512cced4f7dd414fd05cfa9bb7416ee37575caeba46cc1e9b56aef163",
              "cpu_affinity": cpus, "kernel": platform.release(), "background": False,
              "compression": "none", "blocks": [], "stage": "delegation", "verified": False}
    expected = None
    try:
        report["controllers"] = prepare_groups("measure")
        template = json.loads((BUNDLE / "config.json").read_text())
        for pair in range(-1, PAIRS):
            block = {"pair": pair, "sources": {}, "cells": []}
            report["blocks"].append(block)
            assert shutil.disk_usage(WORK).free > 3 << 30
            configs = ["unlimited", "oci-quarter"]
            if pair % 2:
                configs.reverse()
            for i, config in enumerate(configs):
                report["stage"] = [pair, "source", config]
                block["sources"][config] = src = {}
                # Guest order changes every block. Change which engine runs
                # first on the first guest every two blocks to cross both orders.
                engines = ENGINES if (pair // 2 + i) % 2 else ENGINES[::-1]
                expected = capture_both(config, engines, template, expected, src)
                persist(report)
            report["expected_response_sha256"] = expected
            for pos in ORDER:
                policy, engine = CELLS[(pos + pair) % len(CELLS)]
                config = "unlimited" if policy == "unlimited" else "oci-quarter"
                report["stage"] = [pair, policy, engine]
                set_spec(config, template)
                row = {}
                block["cells"].append(row)
                restore(policy, engine, WORK / (config+"-"+engine), expected, block["sources"][config]["pids"], row)
                delete()
                persist(report)
            for config in block["sources"]:
                for engine in ENGINES:
                    shutil.rmtree(WORK / (config+"-"+engine))
        write_bench(report["blocks"])
        measured = [b for b in report["blocks"] if b["pair"] >= 0]
        report["median_paired_graph_over_wire"] = {
            p: {m: statistics.median(b["graph_over_wire"][p][m] for b in measured) for m in METRICS}
            for p in POLICIES}
        report["median_paired_quarter_vs_unlimited_engine_ratio"] = {
            m: statistics.median(b["quarter_vs_unlimited_engine_ratio"][m] for b in measured) for m in METRICS}
        report["verified"] = True
    except Exception as exc:
        report["failure"] = type(exc).__name__ + " " + str(exc)[:1350]
        raise
    finally:
        persist(report)
        call("delete", "--force", CID, check=False)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "host":
        host_caps(sys.argv[2])
    elif sys.argv[1:] == ["preflight"]:
        record = {}
        try:
            record["controllers"] = prepare_groups("preflight")
            record["parent_cpu_max"] = quota(GROUP)
        except Exception as exc:
            record["failure"] = type(exc).__name__ + " " + str(exc)[:1000]
            raise
        finally:
            (OUT / "delegation.json").write_text(json.dumps(record))
    else:
        assert sys.argv[1:] == ["measure"]
        run()
