"""Observe the original mixed cmd TestRunners and an isolated TSYNC control."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse

W = Path(os.environ["RUNNER_TEMP"]) / "mixed-tsync"
OUT = W / "receipt.json"
BASE = "//runsc/cmd:cmd_test"
OWNERS = {BASE: ("amd64", 62), BASE + "_presubmit_arm64": ("arm64", 183)}
REQUIRED = {"default", "fdlimit", "hostinet", "no_directfs", "no_directfs_hostinet"}
MODES = REQUIRED | {"kvm", "coretags"}
TARGETS = W / "two.targets"
BAZEL = [str(W / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx8g"]
FLAGS = [
    "--config=source-presubmit-mixed",
    "--target_pattern_file=" + str(TARGETS),
    "--build_tests_only",
    "--test_arg=-test.run=^(TestBootPosture|TestWorkerTsyncReporting)$",
    "--test_arg=-test.v",
    "--test_env=GO_TEST_WRAP_TESTV=1",
    "--nocache_test_results",
    "--flaky_test_attempts=1",
    "--modify_execution_info=^TestRunner$=+no-remote-cache",
    "--jobs=32",
    "--keep_going",
    "--remote_download_outputs=minimal",
    "--remote_download_regex=.*testlogs/.*/test[.](xml|log)$",
]
IMAGE = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"


def save(doc):
    data = json.dumps(doc, indent=2, sort_keys=True).encode()
    assert len(data) < 200000 and not re.search(
        rb"(?i)authorization\s*[:=]|github_pat_|gh[pousr]_|x-buildbuddy-api-key", data
    )
    OUT.write_bytes(data + b"\n")


def events(path):
    if path.is_file():
        with path.open() as stream:
            for line in stream:
                yield json.loads(line)


def graph(doc):
    from native_graph import project_dump, test_log

    TARGETS.write_text("\n".join(OWNERS) + "\n")
    bep, dump = W / "graph.bep", W / "graph.pb"
    args = [
        *BAZEL,
        "test",
        *FLAGS,
        "--nobuild",
        "--remote_executor=",
        "--remote_cache=",
        "--bes_backend=",
        "--spawn_strategy=local",
        "--strategy=TestRunner=local",
        "--build_event_json_file=" + str(bep),
        "--experimental_aquery_dump_after_build_format=streamed_proto",
        "--experimental_aquery_dump_after_build_output_file=" + str(dump),
    ]
    with (W / "graph.console").open("wb") as log:
        code = subprocess.run(
            args, stdout=log, stderr=subprocess.STDOUT, timeout=1000
        ).returncode
    selected, sizes, finish = {}, {}, []
    for e in events(bep):
        ids = e["id"]
        assert "testResult" not in e and "action" not in e
        if "targetConfigured" in ids and e.get("configured", {}).get("testSize"):
            sizes[ids["targetConfigured"]["label"]] = e["configured"]["testSize"]
        if (
            (t := ids.get("targetCompleted"))
            and not t.get("aspect")
            and e.get("aborted", {}).get("reason") == "NO_BUILD"
        ):
            selected[t["label"]] = t["configuration"]["id"]
        if "finished" in e:
            finish.append(e["finished"].get("exitCode", {}).get("code", 0))
    workers = {}
    doc["selection"] = {
        "exit": code,
        "configured": selected,
        "sizes": sizes,
        "finished": finish,
        "workers": workers,
    }
    save(doc)
    assert code == 1 and finish == [1] and set(selected) == set(sizes) == OWNERS.keys()
    assert all(s.lower() == "medium" for s in sizes.values())
    g = project_dump(dump)
    for a in g["actions"]:
        owner = g["targets"][str(a["targetId"])]
        cfg = g["configs"][str(a["configurationId"])]
        if selected.get(owner) != cfg or not test_log(
            a, g["artifacts"], g["fragments"]
        ):
            continue
        assert owner not in workers
        cpu = OWNERS[owner][0]
        props = {p["key"]: p["value"] for p in a["executionInfo"]}
        assert a["executionPlatform"].lstrip("@") == "//test/rbe:ubuntu_" + cpu
        assert (
            props["Arch"] == cpu
            and props["OSFamily"] == "linux"
            and props["container-image"] == IMAGE
        )
        assert (
            props["workload-isolation-type"] == "firecracker"
            and props["dockerUser"] == "root"
        )
        workers[owner] = {
            "arch": cpu,
            "isolation": "firecracker",
            "user": "root",
            "target_config": cfg,
        }
    doc["eligible"] = workers.keys() == OWNERS.keys()
    assert doc["eligible"]


def execute(doc):
    assert doc["eligible"] and TARGETS.read_text().splitlines() == list(OWNERS)
    inv = str(uuid.uuid4())
    doc["invocation"] = inv
    save(doc)
    args = [
        *BAZEL,
        "--bazelrc=" + str(W / "remote.rc"),
        "test",
        *FLAGS,
        "--invocation_id=" + inv,
        "--build_event_json_file=" + str(W / "run.bep"),
    ]
    with (W / "run.console").open("wb") as log:
        try:
            doc["process_exit"] = subprocess.run(
                args, stdout=log, stderr=subprocess.STDOUT, timeout=2400
            ).returncode
        except subprocess.TimeoutExpired:
            doc["process_exit"] = 124


def original(owner, item):
    name = item["name"]
    uri = urlparse(item["uri"])
    ref = re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)$", uri.path)
    sha, size = (
        (ref[1], int(ref[2]))
        if ref
        else (item.get("digest"), int(item.get("length", 0)))
    )
    candidates = list(
        Path("bazel-out").glob("*/testlogs/" + owner[2:].replace(":", "/") + "/" + name)
    )
    if uri.scheme == "file" and not uri.netloc:
        candidates.append(Path(unquote(uri.path)))
    for path in candidates:
        if path.is_file() and 0 < path.stat().st_size <= 8 << 20:
            data = path.read_bytes()
            h = hashlib.sha256(data).hexdigest()
            if (sha == h and size == len(data)) or (
                not sha
                and uri.scheme == "file"
                and path.resolve() == Path(unquote(uri.path)).resolve()
            ):
                return data, [h, len(data)]
    raise ValueError("missing original report")


def observe(row, owner, log, xml):
    cpu, machine = OWNERS[owner]
    cases = {}
    for case in ET.fromstring(xml).iter("testcase"):
        name = case.get("name", "").removeprefix("cmd.")
        if not (
            name in ("TestWorkerTsyncReporting", "TestBootPosture")
            or re.fullmatch(
                r"TestBootPosture/(default|fdlimit|hostinet|no_directfs|no_directfs_hostinet|kvm|coretags)(/(sentry|gofer))?",
                name,
            )
        ):
            continue
        assert name not in cases
        cases[name] = (
            "FAIL"
            if case.find("failure") is not None or case.find("error") is not None
            else "SKIP"
            if (
                case.find("skipped") is not None
                or case.get("status") in ("notrun", "skipped")
                or case.get("result") in ("skipped", "suppressed")
            )
            else "PASS"
        )
    row["cases"] = cases
    workers = re.findall(
        r"TSYNC_WORKER cpu=(amd64|arm64) machine=(62|183) uid=(\d{1,6}) native=(true|false) release=([A-Za-z0-9._+-]{1,96})\r?$",
        log,
        re.M,
    )
    controls = re.findall(
        r"TSYNC_CONTROL peer_delta=(-?\d{1,5}) installer_delta=(-?\d{1,5}) same_before=([01]) counts=([01]) peer_baseline=(-?1|0) installer_baseline=(-?1|0) peer_enforced=(-?1|0) installer_enforced=(-?1|0)\r?$",
        log,
        re.M,
    )
    row["worker"] = workers
    keys = (
        "peer_delta",
        "installer_delta",
        "same_before",
        "counts",
        "peer_baseline",
        "installer_baseline",
        "peer_enforced",
        "installer_enforced",
    )
    row["controls"] = [dict(zip(keys, map(int, v))) for v in controls]
    phases = {}
    for mode, i, total, above, below in re.findall(
        r"TSYNC_SAMPLE TestBootPosture/([a-z0-9_]+)/sentry index=([0-2]) tasks=(\d{1,6}) above=(\d{1,6}) below=(\d{1,6})\r?$",
        log,
        re.M,
    ):
        if mode in MODES:
            index = int(i)
            samples = phases.setdefault(mode, {"samples": {}})["samples"]
            assert index not in samples and int(above) + int(below) <= int(total)
            samples[index] = {
                "tasks": int(total),
                "above": int(above),
                "below": int(below),
            }
    for mode, shared, persistent, cleared in re.findall(
        r"TSYNC_STABILITY TestBootPosture/([a-z0-9_]+)/sentry shared=(\d{1,6}) persistent=(\d{1,6}) cleared=(\d{1,6})\r?$",
        log,
        re.M,
    ):
        if mode in MODES:
            entry = phases.setdefault(mode, {"samples": {}})
            assert "shared" not in entry and int(persistent) + int(cleared) <= int(
                shared
            )
            entry.update(
                shared=int(shared), persistent=int(persistent), cleared=int(cleared)
            )
    row["sentry"] = phases
    row["behavior_verified"] = (
        cases.get("TestWorkerTsyncReporting") == "PASS"
        and len(controls) == 1
        and all(row["controls"][0][k] == 1 for k in keys[4:])
    )
    row["counts_comparable"] = bool(
        len(controls) == 1
        and row["controls"][0]["counts"]
        and row["controls"][0]["same_before"]
    )
    row["verified"] = (
        row["behavior_verified"]
        and row["counts_comparable"]
        and len(workers) == 1
        and workers[0][:4] == (cpu, str(machine), "0", "true")
        and all(
            cases.get("TestBootPosture/" + m + "/" + role) in ("PASS", "FAIL")
            for m in REQUIRED
            for role in ("sentry", "gofer")
        )
        and all("TestBootPosture/" + m in cases for m in MODES)
        and all(
            m in phases
            and phases[m].get("shared", 0) > 0
            and set(phases[m]["samples"]) == {0, 1, 2}
            for m in REQUIRED
        )
    )


def collect(doc):
    rows = {}
    doc["results"] = rows
    finish = []
    started = []
    for e in events(W / "run.bep"):
        if "started" in e:
            started.append(e["started"].get("uuid"))
        if "finished" in e:
            finish.append(e["finished"].get("exitCode", {}).get("code", 0))
        if "testResult" not in e:
            continue
        ident = e["id"]["testResult"]
        owner = ident["label"].lstrip("@")
        assert owner in OWNERS and owner not in rows
        action = e["testResult"]
        info = action.get("executionInfo", {})
        row = rows[owner] = {
            "status": action.get("status"),
            "strategy": info.get("strategy"),
            "cached_local": bool(action.get("cachedLocally")),
            "cached_remote": bool(info.get("cachedRemotely")),
            "attempt": [ident.get(k, 1) for k in ("run", "shard", "attempt")],
            "reports": {},
            "verified": False,
        }
        save(doc)
        try:
            blobs = {}
            for f in action.get("testActionOutput", []):
                if f["name"] in ("test.log", "test.xml"):
                    assert f["name"] not in blobs
                    blobs[f["name"]], row["reports"][f["name"]] = original(owner, f)
            observe(
                row,
                owner,
                blobs["test.log"].decode(errors="replace").replace("\x16", ""),
                blobs["test.xml"],
            )
        except (OSError, ValueError, KeyError, AssertionError, ET.ParseError) as exc:
            row["collection_error"] = type(exc).__name__
        save(doc)
    doc.update(started=started, finished=finish)
    doc["verified"] = (
        started == [doc.get("invocation")]
        and finish == [doc.get("process_exit")]
        and finish in ([0], [3])
        and rows.keys() == OWNERS.keys()
        and all(
            r["strategy"] == "remote"
            and not r["cached_local"]
            and not r["cached_remote"]
            and r["attempt"] == [1, 1, 1]
            and r["status"] in ("PASSED", "FAILED")
            and r["verified"]
            for r in rows.values()
        )
    )


doc = json.loads(OUT.read_text())
try:
    {"graph": graph, "execute": execute, "collect": collect}[sys.argv[1]](doc)
finally:
    save(doc)
if sys.argv[1] == "collect":
    raise SystemExit(not doc["verified"])
