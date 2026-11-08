"""Count callback registrations during an actual 50,000-file graph restore."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from xml.etree import ElementTree

import live
import recovery


ROOT = live.TMP
OUT = live.OUT
OWNER = "//runsc/container:container_test"
BENCH = "//test/benchmarks/checkpointfile:checkpointfile_test"
LEAF = "TestCheckpointTmpfsTreeForProfile"
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off",
         "--remote_cache=", "--remote_executor=", "--bes_backend=",
         "--spawn_strategy=processwrapper-sandbox,local",
         "--strategy=GoStdlib=processwrapper-sandbox",
         "--strategy=GoStdlibList=processwrapper-sandbox"]


def write(name, value):
    (OUT / name).write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def bazel(command, *args):
    manifest = json.loads((ROOT / "inputs.json").read_bytes())
    recovery.KNOWN.update((entry[2], entry) for entry in manifest)
    recovery.assets.DIST.mkdir(parents=True, exist_ok=True)
    for attempt in range(15):
        dirs = ["--distdir=" + str(p) for p in sorted(recovery.DIRS)]
        cmd = [ROOT / "bazel", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", command,
               *FLAGS, "--experimental_downloader_config=" + str(ROOT / "downloader"), *dirs, *args]
        with (ROOT / "bazel.log").open("w+") as log:
            proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
            log.seek(0)
            raw = log.read()
        if proc.returncode == 0:
            return raw
        errors = [s[:600] for s in raw.splitlines() if "ERROR:" in s or "Error downloading" in s]
        recovered = recovery.recover(raw)
        write("build-diagnostics.json", {"command": command, "attempt": attempt, "exit": proc.returncode,
                                        "diagnostics": errors[-12:], "public_inputs": recovery.RECORD["public_inputs"]})
        if not recovered:
            raise RuntimeError(f"Bazel {command} exited {proc.returncode}")
    raise RuntimeError("public input retry limit")


def default(label):
    paths = [Path(s) for s in bazel("cquery", "--output=files", label).splitlines()
             if s.startswith("bazel-out/") and Path(s).is_file() and os.access(s, os.X_OK)]
    path, = paths
    return path


def verify_graph(graph):
    callbacks = graph["Callbacks"] or []
    batches = graph["Batches"] or []
    fans = graph["Incoming"]
    assert len({(r["Plain"], r["Hooked"]) for r in fans}) == len(fans)
    assert sum(r["Objects"] for r in fans) == graph["Objects"]
    dependency = sum(r["Count"] for r in callbacks if r["Kind"] == "dependency")
    waiting = sum(r["Count"] for r in callbacks if r["Waiting"])
    assert sum(r["Plain"] * r["Objects"] for r in fans) == dependency == graph["PlainRecords"]
    assert sum(r["Hooked"] * r["Objects"] for r in fans) == waiting - dependency
    assert sum(r["Count"] for r in callbacks) == graph["HookRecords"] + dependency == graph["Records"]
    direct = sum(r["Count"] for r in callbacks if r["Kind"] == "dependency"
                 and r["Origin"].endswith((".decodeMap", ".decodeElems")))
    assert sum(r["Count"] * r["Dependencies"] for r in batches) == direct
    assert graph["BatchCapacity"] >= graph["Records"] and graph["CompactBytes"] < graph["RecordBytes"]
    return {"singleton_plain": sum(r["Objects"] for r in fans if (r["Plain"], r["Hooked"]) == (1, 0)),
            "plain_records": dependency, "all_records": graph["Records"],
            "actual_allocated_capacity_bytes": graph["Actual"]["AllocatedSlots"] * graph["RecordBytes"],
            "batch_allocated_capacity_bytes": graph["Batched"]["AllocatedSlots"] * graph["RecordBytes"],
            "split_allocated_capacity_bytes": graph["FullHooks"]["AllocatedSlots"] * graph["RecordBytes"]
            + graph["CompactPlain"]["AllocatedSlots"] * graph["CompactBytes"]}


def run():
    (ROOT / "downloader").write_text(recovery.checkpoint_archive_probe.config("github[.]com", "codeload.github.com"))
    root = live.prepare_root("private-root")
    live.launch(root, True)
    bazel("build", OWNER, BENCH)
    owner, reader = default(OWNER), default(BENCH)
    dest = root / "work/owner"
    dest.mkdir()
    shutil.copy2(owner, dest / "container_test")
    runfiles = dest / "container_test.runfiles"
    shutil.copytree(Path(str(owner) + ".runfiles"), runfiles)
    top = runfiles / "_main"
    runtime = top / "release"
    exe = {"owner": dest / "container_test", "reader": reader, "runsc": runtime / "runsc",
           "sentry": runtime / "gvisor-bin/gvisor_sentry", "guest": live.find_test_app(top)}
    for path in [exe["owner"], runtime / "runsc", *(runtime / "gvisor-bin").iterdir()]:
        if path.is_file():
            live.runtime_libs(root, path)
    write("source.json", {"source": os.environ["GITHUB_SHA"], "pr_head": "9f19447bdd727295bbaee1d8b198ae4e52a75ce5",
                          "previous_census": 13092843079, "executables": {k: live.sha(p) for k, p in exe.items()}})
    (root / "measure.sh").write_text("""set -euo pipefail
export PATH=/bin HOME=/tmp TEST_WORKSPACE=_main TEST_TARGET=//runsc/container:container_test GO_TEST_WRAP_TESTV=1
export TEST_SRCDIR=/work/owner/container_test.runfiles RUNFILES_DIR=/work/owner/container_test.runfiles
export XML_OUTPUT_FILE=/records/guest.xml WORKLOAD_CHECKPOINT_DIR=/work/checkpoint
mkdir "$WORKLOAD_CHECKPOINT_DIR"
cd "$TEST_SRCDIR/$TEST_WORKSPACE"
/work/owner/container_test -test.v '-test.run=^TestCheckpointTmpfsTreeForProfile$' > /records/guest.log 2>&1
""")
    try:
        live.launch(root, False)
    finally:
        for kind in ("xml", "log"):
            p = root / "records" / ("guest." + kind)
            if p.exists():
                shutil.copy2(p, OUT / p.name)
    log = (OUT / "guest.log").read_text()
    test, = ElementTree.parse(OUT / "guest.xml").findall(".//testcase")
    assert test.get("name") == LEAF and not any(test.find(k) is not None for k in ("failure", "error", "skipped"))
    assert "--- PASS: " + LEAF in log and "TMPFS-VERIFIED 50000" in log and "TMPFS-GRAPH kernel=true" in log
    media = root / "work/checkpoint"
    def identity():
        return {name: {"bytes": (media / name).stat().st_size, "sha256": live.sha(media / name)}
                for name in ("checkpoint.img", "pages.img", "pages_meta.img", "spec.json")}
    captured = identity()
    write("guest.json", {"owner": OWNER, "case": LEAF, "media": captured,
                         "guest_log": live.sha(OUT / "guest.log"), "guest_xml": live.sha(OUT / "guest.xml")})
    cmd = [reader.absolute(), "-checkpoint-dir=" + str(media), "-test.cpu=1", "-test.run=^$",
           "-test.bench=^BenchmarkCheckpointFile$/^DecodeStatefile$", "-test.benchtime=1x"]
    measured = subprocess.run(cmd, env={**os.environ, "STATE_CALLBACK_CENSUS": "1", "GO_TEST_WRAP": "0"},
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=360)
    (OUT / "decode.log").write_bytes(measured.stdout)
    text = measured.stdout.decode()
    graphs = [json.loads(s.removeprefix("STATE-CALLBACK-CENSUS ")) for s in text.splitlines()
              if s.startswith("STATE-CALLBACK-CENSUS ")]
    assert measured.returncode == 0 and re.search(r"^PASS$", text, re.M)
    assert len(graphs) == 2 and graphs[0]["Objects"] < 100 and graphs[1]["Objects"] > 50000
    assert captured == identity()
    summaries = [verify_graph(g) for g in graphs]
    write("census.json", {"graphs": graphs, "summaries": summaries, "input": captured,
                          "qualifier": "Physical registrations. Alternative storage costs model allocated slots. No timing comparison."})


if __name__ == "__main__":
    run()
