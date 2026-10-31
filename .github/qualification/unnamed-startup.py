"""Compare a matched pair of retained, hashed OCI Sentries on a native Linux host."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import selectors
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

tmp = Path(os.environ["RUNNER_TEMP"])
proof = tmp / "startup-proof"
proof.mkdir(exist_ok=True)
work = tmp / "startup-work"
work.mkdir(exist_ok=True)
manifest = json.loads((Path(__file__).with_name("startup-manifest.json")).read_text())
variants = ("original-A", "prototype-A")
pairs = 32
seed = 20261031
samples = []
failures = []
clock = time.monotonic_ns


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def save(name, data):
    (proof / name).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare():
    install = work / "release"
    guest = work / "rootfs"
    guest.mkdir()
    for path, entry in manifest["files"].items():
        out = work / path
        out.parent.mkdir(parents=True, exist_ok=True)
        assert entry["host"] in ("remote.buildbuddy.io","remote.buildbuddy.io:443")
        with out.open("wb") as stream:
            subprocess.run([str(tmp / "bb"), "download", "--target=grpcs://" + entry["host"], entry["resource"]],
                           stdout=stream, check=True, timeout=120)
        measured = (digest(out), out.stat().st_size)
        assert measured == (entry["sha256"], entry["size"]), (path, measured, entry)
        out.chmod(0o755)
    common_sentry = install / "gvisor-bin/gvisor_sentry"
    assert digest(common_sentry) == manifest["files"]["original-A"]["sha256"]
    assert manifest["files"]["original-A"]["sha256"] != manifest["files"]["prototype-A"]["sha256"]
    spec = {"ociVersion": "1.0.2", "root": {"path": "rootfs"},
            "process": {"terminal": False, "user": {"uid": 0, "gid": 0}, "cwd": "/",
                        "args": ["/probe", "echo", "guest-ready"], "env": ["PATH=/"]},
            "mounts": [{"type": "proc", "source": "proc", "destination": "/proc"}],
            "linux": {"namespaces": [{"type": t} for t in ("pid", "mount", "ipc", "uts", "network")]}}
    (work / "config.json").write_text(json.dumps(spec))
    save("inputs.json", manifest)
    return install


def ts_ns(rfc3339):
    whole, fraction = re.fullmatch(r"(.{19})(?:\.(\d+))?Z", rfc3339).groups()
    seconds = int(datetime.fromisoformat(whole).replace(tzinfo=timezone.utc).timestamp())
    return seconds * 1_000_000_000 + int((fraction or "").ljust(9, "0"))


def observe(install, variant, name):
    # Keep the installation path fixed; change only the executable inode between finished guests.
    target = install / "gvisor-bin/gvisor_sentry"
    staged = target.with_name("next-sentry")
    if not (work / variant).samefile(target):
        os.link(work / variant, staged)
        os.replace(staged, target)
    timeline = proof / (name + ".timeline.json")
    command = [str(install / "runsc"), "--root=" + str(work / "runtime"), "--platform=systrap", "--network=none",
               "--overlay2=none", "--host-settings=ignore", "--ignore-cgroups",
               "--boot-times-log=" + str(timeline), "run", "--bundle=" + str(work), name]
    started = utcnow()
    with tempfile.TemporaryFile() as stderr:
        start = clock()
        p = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(p.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=30):
                    raise TimeoutError("timed out waiting for first guest output")
                first = os.read(p.stdout.fileno(), 4096)
                got = clock()
            remaining, _ = p.communicate(timeout=30)
            done = clock()
            stderr.seek(0)
            assert p.returncode == 0 and first + remaining == b"guest-ready\n", (p.returncode, first,
                remaining, stderr.read()[-2400:].decode(errors="replace"))
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
    tl = json.loads(timeline.read_text())
    assert not tl.get("invalidReason"), tl
    points = {p["name"]: ts_ns(p["when"]) for p in tl["midpoints"]}
    loader = points["loader created"] - points["creating loader"]
    active = (points["control server started"] - ts_ns(tl["start"])
              + points["kernel started"] - points["start signal received"])
    assert 0 < loader <= active and 0 < got - start <= done - start, (loader, active, got-start, done-start)
    return dict(variant=variant, start_utc=started, finish_utc=utcnow(), loader_ns=loader,
                active_boot_ns=active, guest_ready_ns=got-start, cli_exit_ns=done-start)


def summarize():
    results = {}
    for i, metric in enumerate(("loader_ns", "active_boot_ns", "guest_ready_ns", "cli_exit_ns")):
        raw = {p: {r["variant"]: r[metric] for r in samples if r["pair"] == p} for p in range(pairs)}
        matches = [[r[v] for v in variants] for r in raw.values() if len(r) == 2]
        if not matches:
            continue
        a, b = zip(*matches)
        differences = [y-x for x, y in matches]
        rng = random.Random(seed+i)
        medians = sorted(statistics.median(rng.choices(differences, k=len(matches))) for _ in range(10000))
        results[metric] = dict(n=len(matches), original_median_ns=statistics.median(a),
                prototype_median_ns=statistics.median(b), paired_median_delta_ns=statistics.median(differences),
                paired_median_bootstrap_95_ns=[medians[249], medians[9749]],
                sum_delta_ns=sum(differences), sum_original_ns=sum(a),
                paired_median_change_percent=statistics.median(100*(y-x)/x for x,y in matches))
    return results


if sys.argv[1:] == ["--prepare"]:
    try:
        prepare()
    except Exception as exc:
        save("verdict.json", dict(stage="content-addressed fetch", failure=repr(exc), executed=0, expected=pairs*2))
        raise
    sys.exit(0)

orders = [variants, variants[::-1]] * (pairs//2)
random.Random(seed).shuffle(orders)
save("plan.json", {"pairs": pairs, "seed": seed, "orders": orders, "warmups": [variants, variants[::-1]],
                   "cpu_affinity": sorted(os.sched_getaffinity(0)), "kernel": list(os.uname()),
                   "sources": manifest["sources"], "started_utc": utcnow()})
try:
    install = work / "release"
    for pair, order in [(-2, variants), (-1, variants[::-1]), *enumerate(orders)]:
        for position, variant in enumerate(order):
            name = ("warm" if pair < 0 else "pair") + f"-{abs(pair):02d}-{position}-{variant.lower()}"
            try:
                row = observe(install, variant, name)
                samples.append(dict(pair=pair, position=position, **row))
            except Exception as exc:
                failures.append(dict(pair=pair, position=position, variant=variant, error=repr(exc)))
            save("raw.json", samples)
            save("failures.json", failures)
        if pair < 0 and failures:
            break
except Exception as exc:
    failures.append(dict(stage="setup", error=repr(exc)))
present = {(r["pair"],r["variant"]) for r in samples if r["pair"] >= 0}
missing = [[p,v] for p in range(pairs) for v in variants if (p,v) not in present]
save("verdict.json", dict(verified=utcnow(), expected=pairs*2, executed=len(present), missing=missing,
                          skips=[], failures=failures, metrics=summarize()))
assert not failures and not missing, (failures, missing)
