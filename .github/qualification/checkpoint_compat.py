"""Compare genuine native runtime pairs and their saved files. Publish typed evidence only."""
import json
import os
from pathlib import Path
import platform
import re
import runpy
import shutil
import subprocess
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

WORK = Path(os.environ["RUNNER_TEMP"]) / "cmd-guest-fixture"
WRAP = WORK / "cmd_guest.py"
H = runpy.run_path(str(WRAP))
BASE = "9ee2906f518e1d0f9fd109e9e1feb2812096418b"
FIX = "36c45f9744fd0ee7d52e070477b1b20616d8e24b"
LATE = "8ee2ad62547dd43cb2980c1a5c5f80281880f760"
GUEST = "c5792be31aa3b9c4d872c1b7d3ae863108c02485"
STAMP = "qualification-memoryfile-compat"
CMD = "//runsc/cmd:cmd_test"
ALLOC = "//pkg/sentry/pgalloc:pgalloc_test"
ENVELOPE = "//pkg/state/statefile:statefile_test"
SPLIT = "TestAsyncSaveToPreservesContents"
COMPAT = "TestCheckpointFormatCompatibility"
ARMS = {"old-inline-to-new", "new-inline-to-new", "new-split-to-old", "new-filesystem-to-old"}
SPLIT_ROWS = {"memfd", "memfd_excluding_committed_zero_pages", "disk-backed"}
FLAGS = ["--config=aarch64", "-c", "opt", "--strip=never", "--jobs=3", "--local_resources=memory=9900",
         "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
         "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
         "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
         "--noshow_progress", "--color=no", "--curses=no"]
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3000m"]


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def select(commit: str) -> None:
    subprocess.run(["git", "checkout", "-q", "--detach", commit], check=True)
    subprocess.run(["git", "tag", "-f", STAMP, commit], check=True)
    assert git("rev-parse", "HEAD") == commit
    assert subprocess.check_output(["tools/workspace_status.sh"], text=True).strip() == "STABLE_VERSION " + STAMP


def run(name: str, args: list[str]) -> tuple[int, Path]:
    log = WORK / (name + ".log")
    with log.open("w") as out:
        code = subprocess.run(args, stdout=out, stderr=subprocess.STDOUT).returncode
    return code, log


def output_files(label: str, action: dict) -> dict[str, Path]:
    package, name = label[2:].split(":")
    paths = {}
    for entry in action["testActionOutput"]:
        fname = entry["name"]
        if fname not in {"test.log", "test.xml"}:
            continue
        uri = urlparse(entry["uri"])
        assert uri.scheme == "file" and fname not in paths
        path = Path(unquote(uri.path))
        assert path.parts[-len(package.split("/")) - 2:] == (*package.split("/"), name, fname)
        assert not entry.get("digest") or H["digest"](path) == entry["digest"]
        paths[fname] = path
    assert paths.keys() == {"test.log", "test.xml"}
    return paths


def stage_release(row: dict, variant: str = "old-release") -> None:
    row["exit"], _ = run(variant + "-build", [*BAZEL, "build", *FLAGS, "//:release"])
    assert row["exit"] == 0
    paths = subprocess.check_output([*BAZEL, "cquery", *FLAGS, "//:release", "--output=files"], text=True).splitlines()
    names = {"runsc", "containerd-shim-runsc-v1", *("gvisor-bin/" + name for name in H["SIDECARS"])}
    assert len(paths) == len(names)
    originals = {}
    dest = WORK / variant
    for value in paths:
        path = Path(value)
        before, sep, short = str(path).partition("/bin/release/")
        assert sep and before.startswith("bazel-out/") and short in names and short not in originals
        facts = H["elf"](path)
        target = dest / short
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        assert target.is_file() and os.access(target, os.X_OK) and H["digest"](target) == facts["sha256"]
        originals[short] = facts
    assert originals.keys() == names
    row["release"] = originals
    row["verified"] = True


def run_test(name: str, label: str, wanted: set[str], pattern: str, row: dict) -> str:
    bep = WORK / (name + ".bep")
    args = [*BAZEL, "test", *FLAGS, "--strategy=TestRunner=standalone", "--nocache_test_results",
            "--flaky_test_attempts=1", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_env=CGROUPV2=true",
            "--test_timeout=900", "--test_filter=" + pattern, "--test_arg=-test.run=" + pattern,
            "--test_output=errors", "--build_event_json_file=" + str(bep)]
    if label == CMD:
        args.append("--run_under=/usr/bin/python3 -IB " + str(WRAP) + " under")
    row["exit"], _ = run(name, [*args, label])
    actions = []
    for line in bep.read_text().splitlines():
        event = json.loads(line)
        if "testResult" in event and event["id"].get("testResult", {}).get("label") == label:
            actions.append(event["testResult"])
    action, = actions
    files = output_files(label, action)
    log = files["test.log"].read_text(errors="replace")
    cases = {}
    for item in ET.parse(files["test.xml"]).iter("testcase"):
        case = item.attrib["name"].removeprefix(label.rsplit("/", 1)[-1].split(":")[0] + ".")
        assert case not in cases
        cases[case] = ("failed" if item.find("failure") is not None or item.find("error") is not None else
                       "skipped" if item.find("skipped") is not None else "pass")
    row.update(label=label, cases=cases, status=action["status"], cached=action.get("cachedLocally", False),
               strategy=action.get("executionInfo", {}).get("strategy"),
               original={k: H["digest"](v) for k, v in files.items()})
    # Attribute typed stages to the actual failing Go subtest while retaining
    # the original local artifact digests. No stdout, statefile or guest bytes.
    row["failure_locations"] = sorted(set(re.findall(r"\b(checkpoint_format_compat_test\.go:\d+):", log)))[:24]
    row["diagnostic_classes"] = sorted(k for k in ("permission denied", "no such file or directory",
        "restore validation failed", "connection refused", "unexpected recipe stamp", "unexpected checkpoint format",
        "generic dump missed", "file contents", "error opening", "could not open", "no checkpoint state",
        "runsc version does not match", "unsupported checkpoint payload format", "starting container",
        "operation not permitted", "filestore") if k in log.lower())
    row["case_verdict"] = (row["exit"] == 0 and action["status"] == "PASSED" and not row["cached"] and
                           row["strategy"] in {"standalone", "local"} and set(cases) == wanted and
                           all(val == "pass" and "--- PASS: " + key in log for key, val in cases.items()))
    return log


def split_observer(name: str, row: dict) -> None:
    source = Path("pkg/sentry/pgalloc/save_restore_test.go")
    before = source.read_text()
    point = "metadata, pages := save(tc.exclude)"
    observe = (point + '\n\t\t\tt.Logf("COMPAT_SPLIT case=%s metadata=%x/%d pages=%x/%d", '
               't.Name(), sha256.Sum256(metadata), len(metadata), sha256.Sum256(pages), len(pages))')
    assert before.count(point) == 1
    changed = before.replace(point, observe)
    if '"crypto/sha256"' not in changed:
        changed = changed.replace("import (", 'import (\n\t"crypto/sha256"', 1)
    try:
        source.write_text(changed)
        wanted = {SPLIT, *(SPLIT + "/" + k for k in SPLIT_ROWS)}
        log = run_test(name, ALLOC, wanted, "^" + SPLIT + "$", row)
        entries = {}
        for case, m, ms, p, ps in re.findall(
                r"COMPAT_SPLIT case=TestAsyncSaveToPreservesContents/(\S+) metadata=([0-9a-f]{64})/(\d+) pages=([0-9a-f]{64})/(\d+)", log):
            assert case not in entries
            entries[case] = {"metadata": m, "metadata_size": int(ms), "pages": p, "pages_size": int(ps)}
        row["products"] = entries
        row["verified"] = row["case_verdict"] and entries.keys() == SPLIT_ROWS
    finally:
        source.write_text(before)


def guest(row: dict) -> None:
    wanted = {COMPAT, *(COMPAT + "/" + name for name in ARMS),
              COMPAT + "/new-inline-to-new/unknown-format", COMPAT + "/new-inline-to-new/different-release"}
    log = run_test("guest", CMD, wanted, "^" + COMPAT + "$", row)
    obs, = [json.loads(line.partition("CMD_GUEST_ORIGINAL ")[2]) for line in log.splitlines()
            if line.startswith("CMD_GUEST_ORIGINAL ")]
    row["namespace_observation"] = obs
    restores = re.findall(r'COMPAT_RESTORED image=([a-f0-9]{64}) format="([^"]*)"', log)
    dumps = re.findall(r"COMPAT_DUMP image=([a-f0-9]{64}) text=([a-f0-9]{64})", log)
    row.update(restores=restores, dumps=dumps, restored_witnesses=log.count("COMPAT_GUEST pid=1 files=2 bytes_each=8192"))
    row["verified"] = (row["case_verdict"] and obs["inner_exit"] == 0 and obs["uname"] == "aarch64" and
                       obs["test_app"]["machine"] == 183 and obs["sidecars"] == 7 and
                       len(restores) == 3 and len(set(image for image, _ in restores)) == 3 and len(dumps) == 1 and
                       (dumps[0][0], "framed-memoryfile-v1") in restores and row["restored_witnesses"] == 4)


def with_order_observer(row: dict, callback) -> None:
    source = Path("runsc/boot/controller.go")
    before = source.read_text()
    # Observe the established pointer exactly before the original failure path
    # clears it. No scheduling, reader/loader behavior, or runtime inputs vary.
    target = '\t\t\tcu.Clean() // Release `cm.l.mu` as onRestoreFailed will acquire it.'
    assert before.count(target) == 1
    observation = ('\t\t\tretErr = fmt.Errorf("qualification split loader installed=%t: %w", '
                   'cm.restorer != nil && cm.restorer.asyncMFLoader != nil, retErr)\n')
    try:
        source.write_text(before.replace(target, observation + target))
        assert subprocess.check_output(["tools/workspace_status.sh"], text=True).strip() == "STABLE_VERSION " + STAMP + "-dirty"
        row["observed_controller_sha256"] = H["digest"](source)
        callback(row)
    finally:
        source.write_text(before)


def split_order(row: dict) -> None:
    root = "TestSplitFormatRejectionBeforePages"
    expected = {root, *(root + "/" + leaf for leaf in ("valid-split", "previous", "corrected"))}
    log = run_test("split-restore-order", CMD, expected, "^" + root + "$", row)
    observations = re.findall(r"SPLIT_ORDER arm=(previous|corrected) installed=(true|false) checkpoint=([0-9a-f]{64}) metadata=([0-9a-f]{64}) pages=([0-9a-f]{64})", log)
    row["order_observations"] = observations
    namespace, = [json.loads(line.partition("CMD_GUEST_ORIGINAL ")[2]) for line in log.splitlines()
                  if line.startswith("CMD_GUEST_ORIGINAL ")]
    row["namespace_observation"] = namespace
    row["verified"] = (row["case_verdict"] and len(observations) == 2 and
                       {item[:2] for item in observations} == {("previous", "true"), ("corrected", "false")} and
                       len({item[2:] for item in observations}) == 1 and
                       namespace["inner_exit"] == 0 and namespace["uname"] == "aarch64")


def main() -> None:
    carrier = git("rev-parse", "HEAD")
    assert [git("rev-parse", "HEAD" + "^" * i) for i in (1, 2, 3)] == [GUEST, FIX, BASE]
    assert platform.machine() == "aarch64"
    rows = {}
    report = {"baseline": BASE, "writer": FIX, "integration": GUEST, "qualification": carrier,
              "kernel": platform.release(), "arch": platform.machine(), "runs": rows}
    receipt = WORK / "result.json"

    def record(name, callback):
        row = rows[name] = {"verified": False}
        try:
            callback(row)
        except (AssertionError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as err:
            row["observer_error"] = type(err).__name__
        finally:
            receipt.write_text(json.dumps(report, indent=2))

    select(BASE)
    record("original_release", stage_release)
    record("original_split", lambda row: split_observer("original-split", row))
    select(carrier)
    record("corrected_split", lambda row: split_observer("corrected-split", row))
    for label, test in [(ALLOC, "TestInlineMetadataFrame"), (ENVELOPE, "TestPayloadFormat")]:
        def component(row, label=label, test=test):
            run_test(test, label, {test}, "^" + test + "$", row)
            row["verified"] = row["case_verdict"]
        record(test, component)
    if rows["original_release"]["verified"]:
        record("paired_guests", guest)
    select(LATE)
    record("late_observer_release", lambda row: with_order_observer(row, lambda got: stage_release(got, "late-release")))
    select(carrier)
    if rows["late_observer_release"]["verified"] and rows["original_release"]["verified"]:
        record("split_order", lambda row: with_order_observer(row, split_order))
    report["late_source"] = LATE
    old_sentry = rows["original_release"].get("release", {}).get("gvisor-bin/gvisor_sentry", {}).get("sha256")
    new_sentry = rows.get("paired_guests", {}).get("namespace_observation", {}).get("current_sidecars", {}).get("gvisor_sentry", {}).get("sha256")
    report["distinct_real_sentries"] = bool(old_sentry and new_sentry and old_sentry != new_sentry)
    matches = (rows["original_split"]["verified"] and rows["corrected_split"]["verified"] and
               rows["original_split"]["products"] == rows["corrected_split"]["products"])
    report["three_split_layouts_identical"] = matches
    report["verified"] = matches and report["distinct_real_sentries"] and {"paired_guests", "split_order"}.issubset(rows) and all(r["verified"] for r in rows.values())
    receipt.write_text(json.dumps(report, indent=2))
    assert receipt.stat().st_size < 32768 and report["verified"]


if __name__ == "__main__":
    main()
