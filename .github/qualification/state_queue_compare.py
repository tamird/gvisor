"""Compare inline and uniform graph waiters on the same native worker."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import traceback
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import census
import live


PARENT = "9f19447bdd727295bbaee1d8b198ae4e52a75ce5"
BASE = "958ce2042477cf4f0d4746ff17c91c4ad0f56859"
FIX = "c6ff07a265acedc54710f37c0e08b983cd839908"
DONOR = "2ca2cf90cd9ca82bed176418d40970728fc212ab"
ROOT, OUT = census.ROOT, census.OUT
OWNERS = {
    "//pkg/state:state_test": ["TestGraphDependencyCycle", "TestGraphCallbackOrder", "TestGraphDecodeMapPopulatedBeforeCallbacks",
                               "TestGraphDecodeMapAndLoadValueDirectStructWithHooks"],
    "//pkg/state/tests:tests_test": ["TestWaitOrdering", "TestLoadHooks", "TestBothEnginesStatefulCallbacks"],
}
ARMS = ("stock", "fixed")
ROUNDS = 16
SAMPLES = {arm: [] for arm in ARMS}
LABELS = {"stock": "inline", "fixed": "uniform"}
PUBLIC = "//pkg/state/tests:tests_test"
CORE = "//pkg/state:state_test"
RECORD = {"parent": PARENT, "inline": BASE, "uniform": FIX, "arms": LABELS, "fixture": DONOR,
          "previous_artifact": 13640319931, "builds": {}, "checks": {}, "errors": {}}


def save():
    census.write("qualification.json", RECORD)


def copy_output(path, name):
    assert path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= (16 << 20), path
    dest = OUT / name
    shutil.copyfile(path, dest)
    return live.sha(dest)


def state_tests(arm, mode, label):
    key = f"{arm}-{mode}-{label.split(':')[1]}"
    bep = ROOT / (key + ".bep")
    flags = ["--config=race"] if mode == "race" else []
    if label == CORE:
        flags.extend(["--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v"])
    else:
        # The previous verbose public suite wrote its complete XML and Go PASS
        # before Bazel timed it out. Here require a successful whole owner.
        flags.append("--test_env=GO_TEST_WRAP_TESTV=0")
    try:
        census.bazel("test", *flags, "--nocache_test_results", "--strategy=TestRunner=local",
                     "--test_timeout=300", "--build_event_json_file=" + str(bep), label)
    finally:
        reports = RECORD["checks"][key] = {}
        if bep.exists():
            for line in bep.read_text().splitlines():
                event = json.loads(line)
                ident = event["id"].get("testResult")
                if not ident:
                    continue
                assert ident["label"] == label and label not in reports
                result = event["testResult"]
                name = f"{key}-{label.split(':')[1]}"
                row = reports[label] = {"id": ident, "status": result["status"], "outputs": {}}
                for output in result.get("testActionOutput", []):
                    filename = output["name"]
                    if filename not in ("test.log", "test.xml"):
                        continue
                    uri = urlsplit(output["uri"])
                    assert uri.scheme == "file" and not uri.netloc
                    src = Path(unquote(uri.path)).resolve(strict=True)
                    digest = copy_output(src, name + "-" + filename)
                    assert digest == output.get("digest", digest)
                    assert src.stat().st_size == int(output.get("length", src.stat().st_size))
                    row["outputs"][filename] = digest
                save()
                assert not result.get("cachedLocally") and not result.get("executionInfo", {}).get("cachedRemotely")
                assert set(row["outputs"]) == {"test.log", "test.xml"}
                cases = ElementTree.parse(OUT / (name + "-test.xml")).findall(".//testcase")
                log = (OUT / (name + "-test.log")).read_text()
                assert re.search(r"^PASS$", log, re.M)
                row["reported_cases"] = len(cases)
                if label == CORE:
                    for expected in OWNERS[label]:
                        assert any(t.get("name", "").split("/")[0] == expected and
                                   all(t.find(k) is None for k in ("failure", "error", "skipped")) for t in cases), expected
                        assert re.search(r"--- PASS: " + expected + r" \(", log), expected
                assert not any(t.find("failure") is not None or t.find("error") is not None for t in cases)
                if label == CORE:
                    shape = "24 64 12" if arm == "fixed" else "24 72 0"
                    trace, = re.findall(r"QUEUE-SHAPE " + shape + r" ORDER (\S+)", log)
                    assert len(trace.split(",")) == 20
                    row["callback_order"] = trace
                    counts = re.findall(r"QUEUE-STATS (\S+) (\d+)", log)
                    assert counts and len(dict(counts)) == len(counts)
                    row["stats_counts"] = dict(counts)
                assert row["status"] == "PASSED"
        assert set(reports) == {label}


def attempt(name, fn, *args):
    try:
        fn(*args)
    except Exception:
        RECORD["errors"][name] = traceback.format_exc()[-4000:]
    finally:
        save()


def build_arm(root, arm, source):
    live.checkout(source)
    fixture = ("runsc/container/BUILD", "runsc/container/container_save_restore_test.go")
    order = Path("pkg/state/graph_decode_callbacks_test.go")
    bench = Path("pkg/state/tests/bench_test.go")
    for path in fixture:
        Path(path).write_bytes(live.git("show", DONOR + ":" + path))
    old = order.read_text()
    marker = "\t// Verify exact computed state propagation:"
    assert old.count(marker) == 1
    call = "if _, err := loadGraph(ctx, &buf, &loaded, hdr.Length, 0); err != nil {"
    assert old.count(call) == 1
    observed = old.replace(call, "CollectStats(true)\n\tdefer CollectStats(false)\n\t"
                           "stats, err := loadGraph(ctx, &buf, &loaded, hdr.Length, 0)\n\tif err != nil {")
    order.write_text(observed.replace(marker,
        '\tfor typ, entry := range stats.graph.byType {\n\t\tname := "state.default"\n'
        '\t\tif typ != nil { name = typ.typ.String() }\n\t\tt.Logf("QUEUE-STATS %s %d", name, entry.count)\n\t}\n'
        '\tvar linkBytes uintptr\n\tif f, ok := reflect.TypeFor[graphDecodeState]().FieldByName("links"); ok { linkBytes = f.Type.Elem().Size() }\n'
        '\tt.Logf("QUEUE-SHAPE %d %d %d ORDER %s", reflect.TypeFor[graphObjectDecodeState]().Size(), '
        'reflect.TypeFor[decodeCallback]().Size(), linkBytes, strings.Join(events, ","))\n' + marker))
    # Use the existing pointer fixture without a next node. Its generated
    # AfterLoad schedules one callback even with no pointers to follow.
    text = bench.read_text()
    entry = '\t"ptr": {\n\t\tNew: buildPtrObject,'
    assert text.count(entry) == 1
    bench.write_text(text.replace(entry, '\t"one_afterload": {New: func(int) any { return buildPtrObject(0) }},\n' + entry))
    try:
        for mode in ("normal", "race"):
            attempt(arm + "-" + mode + "-core", state_tests, arm, mode, CORE)
        if arm == "fixed":
            attempt("uniform-normal-public", state_tests, arm, "normal", PUBLIC)
        attempt(arm + "-race-public", state_tests, arm, "race", PUBLIC)
        census.bazel("build", census.OWNER, census.BENCH, PUBLIC)
        owner = census.default(census.OWNER)
        dest = root / "work" / arm
        dest.mkdir()
        shutil.copy2(owner, dest / "container_test")
        runfiles = dest / "container_test.runfiles"
        subprocess.run(["cp", "-rL", str(owner) + ".runfiles", str(runfiles)], check=True)
        reader = ROOT / (arm + "-decode")
        shutil.copy2(census.default(census.BENCH), reader)
        ordinary = ROOT / (arm + "-ordinary")
        shutil.copy2(census.default(PUBLIC), ordinary)
        top = runfiles / "_main"
        release = top / "release"
        app = live.find_test_app(top)
        built_app = live.sha(app)
        shared = root / "work/test_app"
        if arm == "stock":
            shared.hardlink_to(app)
        else:
            app.unlink()
            app.hardlink_to(shared)
        paths = {"owner": dest / "container_test", "reader": reader, "ordinary": ordinary, "runsc": release / "runsc",
                 "sentry": release / "gvisor-bin/gvisor_sentry", "test_app": app}
        for binary in paths.values():
            with binary.open("rb") as f:
                header = f.read(20)
            assert header[:6] == b"\x7fELF\x02\x01" and header[18:20] == b"\x3e\x00"
        for path in [paths["owner"], paths["runsc"], *(release / "gvisor-bin").iterdir()]:
            if path.is_file():
                live.runtime_libs(root, path)
        RECORD["builds"][arm] = {"source": source, "built_test_app": built_app,
                                 "outputs": {k: live.sha(p) for k, p in paths.items()},
                                 "fixtures": {p: live.sha(Path(p)) for p in (*fixture, str(order), str(bench))}}
        save()
    finally:
        live.git("restore", "--", *fixture, str(order), str(bench))


def guests(root):
    donor = (ROOT / "payload.sh").read_text()
    loop = donor[donor.index("for ((round=0; round<=16; ++round)); do"):]
    # Keep the first stock capture for both read-only decoders and the original
    # test logs. The fixture times and validates every restore in both arms.
    def replace(old, new):
        nonlocal loop
        assert loop.count(old) == 1
        loop = loop.replace(old, new)
    replace('    echo "$code"', '    cp /work/case.log "$records/$name.log"\n    echo "$code"')
    replace('cp "$XML_OUTPUT_FILE" "$outer_xml"',
            'if [[ $name == r0-stock ]]; then cp -r "$WORKLOAD_CHECKPOINT_DIR" /work/frozen; fi')
    (root / "measure.sh").write_text(
        "set -euo pipefail\nexport PATH=/bin HOME=/tmp TEST_WORKSPACE=_main "
        "TEST_TARGET=//runsc/container:container_test GO_TEST_WRAP_TESTV=1\nrecords=/records\n" + loop)
    try:
        live.launch(root, False)
    finally:
        for path in sorted((root / "records").iterdir()):
            if re.fullmatch(r"r(?:[0-9]|1[0-6])-(?:stock|fixed)\.(?:xml|log|markers|exit|bytes)", path.name):
                copy_output(path, path.name)
    RECORD["guest_host"] = live.row["host"]
    for i in range(ROUNDS + 1):
        for arm in ARMS:
            name = f"r{i}-{arm}"
            test, = ElementTree.parse(OUT / (name + ".xml")).findall(".//testcase")
            assert test.get("name") == census.LEAF and all(test.find(k) is None for k in ("failure", "error", "skipped"))
            markers = (OUT / (name + ".markers")).read_text().splitlines()
            assert (OUT / (name + ".exit")).read_text().strip() == "0"
            assert {"TMPFS-VERIFIED 50000", "TMPFS-GRAPH kernel=true"} <= set(markers)
            ns = dict(re.fullmatch(r"TMPFS-NS (\S+) ([1-9]\d*)", s).groups() for s in markers if s.startswith("TMPFS-NS "))
            assert 0 < int(ns["restore"]) < int(ns["new-to-guest"])
            if i:
                for phase in ("restore", "new-to-guest"):
                    SAMPLES[arm].append(f"BenchmarkGuest/{phase} 1 {ns[phase]} ns/op")


def decode(root):
    media = root / "work/frozen"
    def identity():
        return {f: live.sha(media / f) for f in ("checkpoint.img", "pages.img", "pages_meta.img", "spec.json")}
    RECORD["decode_image"] = identity()
    cpu = min(os.sched_getaffinity(0))
    for i in range(ROUNDS + 1):
        for arm in (ARMS if i == 0 or i % 2 else reversed(ARMS)):
            reader = ROOT / (arm + "-decode")
            assert live.sha(reader) == RECORD["builds"][arm]["outputs"]["reader"]
            cmd = ["taskset", "-c", str(cpu), str(reader), "-checkpoint-dir=" + str(media), "-test.cpu=1",
                   "-test.run=^$", "-test.bench=^BenchmarkCheckpointFile$/^DecodeStatefile$", "-test.benchtime=1x", "-test.benchmem"]
            out = OUT / f"decode-{i}-{arm}.log"
            with out.open("wb") as log:
                proc = subprocess.run(cmd, env={**os.environ, "GO_TEST_WRAP": "0", "GOMAXPROCS": "1", "GOGC": "100"},
                                      stdout=log, stderr=subprocess.STDOUT, timeout=240)
            data = out.read_text()
            assert proc.returncode == 0 and re.search(r"^PASS$", data, re.M)
            row, = re.findall(r"^BenchmarkCheckpointFile/DecodeStatefile(?:-1)?\s+1\s+.+$", data, re.M)
            assert {"ns/op", "B/op", "allocs/op"} <= set(row.split()[3::2])
            if i:
                SAMPLES[arm].append(row)
    assert identity() == RECORD["decode_image"]


def ordinary():
    cpu = min(os.sched_getaffinity(0))
    for i in range(ROUNDS + 1):
        for arm in (ARMS if i == 0 or i % 2 else reversed(ARMS)):
            exe = ROOT / (arm + "-ordinary")
            assert live.sha(exe) == RECORD["builds"][arm]["outputs"]["ordinary"]
            # Run one input per process so an earlier sub-benchmark cannot
            # populate the decoder pool before the cold 1x observation.
            for shape in ("one_afterload", "ptr"):
                for mode, duration in (("cold", "1x"), ("pooled", "200ms")):
                    cmd = ["taskset", "-c", str(cpu), str(exe), "-test.cpu=1", "-test.run=^$",
                           f"-test.bench=^BenchmarkDecoding$/^{shape}$/^state$", "-test.benchtime=" + duration, "-test.benchmem"]
                    out = OUT / f"ordinary-{i}-{arm}-{shape}-{mode}.log"
                    with out.open("wb") as log:
                        proc = subprocess.run(cmd, env={**os.environ, "GO_TEST_WRAP": "0", "GOMAXPROCS": "1", "GOGC": "100"},
                                              stdout=log, stderr=subprocess.STDOUT, timeout=60)
                    data = out.read_text()
                    assert proc.returncode == 0 and re.search(r"^PASS$", data, re.M)
                    name = "BenchmarkDecoding/" + shape + "/state"
                    row, = re.findall(r"^" + name + r"(?:-1)?\s+\d+\s+.+$", data, re.M)
                    n = int(row.split()[1])
                    assert (n == 1 if mode == "cold" else n > 1) and {"ns/op", "B/op", "allocs/op"} <= set(row.split()[3::2])
                    if i:
                        SAMPLES[arm].append(row.replace(name, name + "/" + mode, 1))


def main():
    assert all(live.git("rev-parse", source + "^").decode().strip() == PARENT for source in (BASE, FIX))
    (ROOT / "downloader").write_text(census.recovery.checkpoint_archive_probe.config("github[.]com", "codeload.github.com"))
    preflight = live.prepare_root("preflight")
    live.launch(preflight, True)
    root = live.prepare_root("trial")
    live.checkout(PARENT)
    attempt("parent-race-public", state_tests, "parent", "race", PUBLIC)
    for arm, source in zip(ARMS, (BASE, FIX), strict=True):
        build_arm(root, arm, source)
    a, b = (RECORD["builds"][arm] for arm in ARMS)
    assert a["fixtures"] == b["fixtures"] and a["outputs"]["test_app"] == b["outputs"]["test_app"]
    assert all(a["outputs"][k] != b["outputs"][k] for k in ("sentry", "reader"))
    attempt("guests", guests, root)
    if (root / "work/frozen").is_dir():
        attempt("decode", decode, root)
    attempt("ordinary", ordinary)
    for arm in ARMS:
        (OUT / (LABELS[arm] + ".bench")).write_text("\n".join(SAMPLES[arm]) + "\n")
    module = Path("MODULE.bazel")
    original = module.read_bytes()
    try:
        module.write_bytes(original + b"\n" + (ROOT / "benchstat.module").read_bytes())
        label = "@org_golang_x_perf//cmd/benchstat:benchstat"
        census.bazel("build", label)
        with (OUT / "benchstat.txt").open("wb") as out:
            subprocess.run([census.default(label).absolute(), OUT / "inline.bench", OUT / "uniform.bench"],
                           stdout=out, check=True)
    finally:
        module.write_bytes(original)
    assert not RECORD["errors"], RECORD["errors"]
    assert all(len(rows) == 7 * ROUNDS for rows in SAMPLES.values())
    cores = [r[CORE] for r in RECORD["checks"].values() if CORE in r]
    assert len(cores) == 4 and len({r["callback_order"] for r in cores}) == 1
    assert len({tuple(sorted(r["stats_counts"].items())) for r in cores}) == 1
    RECORD["verified"] = True
    save()


if __name__ == "__main__":
    main()
