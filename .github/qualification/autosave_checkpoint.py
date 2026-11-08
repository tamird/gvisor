"""Compare the release CLI and the actual autosave formatter on the same guest files."""

import json
import os
from pathlib import Path
import platform
import re
import shutil
import traceback
import subprocess
import sys

WORK = Path(os.environ["RUNNER_TEMP"]) / "cmd-guest-fixture"
sys.path.insert(0, str(WORK))
import checkpoint_compat as fixture
import tldk_public_inputs as public_inputs

PUBLISHED = "ebcea451de61a61957c82527fa94241827267cde"
PRODUCT = "912702589414e631909e449f5926b66fa6d5a2c1"
CARRIER = os.environ["GITHUB_SHA"]
TRANSFER = WORK / "transfer"
REPORT = {"published": PUBLISHED, "product": PRODUCT, "carrier": CARRIER, "runs": {}, "inputs": [], "verified": False}
REPORT["prior_layouts"] = {
    "artifact": "https://github.com/tamird/gvisor/actions/runs/42499334480/artifacts/13624733452",
    "archive_sha256": "cbd56cb8c483346a12fec0eb0005ffd6f3db8c9191e6a6a146f2fe6227c4bd38",
    "owner": "//runsc/cmd/sentry/forwardcmd:forwardcmd_test",
    "test": "TestStatefileDumpMemoryFiles",
    "passed_leaves": 13,
    "test_xml_sha256": "8f653952ef3498ea56f9176cd86ab2691f96b2f88817c1550b365a053c1ef081",
}
REPORT["prior_cli_and_guests"] = {"artifact": "https://github.com/tamird/gvisor/actions/runs/42501078883/artifacts/13625508398",
                                 "archive_sha256": "4520819836aabb16cadde647a331e2133bd72dfe779d251d31082b42fd6f00e8"}
RECEIPT = WORK / "result.json"
INPUTS = json.loads((WORK / "inputs.json").read_text())
ATTEMPTED = set()
original_run = fixture.run
original_outputs = fixture.output_files


def capture_outputs(label, action):
    files = original_outputs(label, action)
    name = {fixture.CMD: "guest", "//test/runner:runner_test": "autosave"}[label]
    dest = WORK / "original"
    dest.mkdir(exist_ok=True)
    for filename, path in files.items():
        copy = dest / (name + "-" + filename)
        shutil.copyfile(path, copy)
        assert fixture.H["digest"](copy) == fixture.H["digest"](path)
    return files


def markers(log, tag):
    rows = []
    for line in log.splitlines():
        _, separator, values = line.partition(tag + " ")
        if separator:
            rows.append(dict(field.split("=", 1) for field in values.split()))
    return rows


def save():
    RECEIPT.write_text(json.dumps(REPORT, indent=2))


def admit(rows):
    rows = [r for r in rows if r[2] not in ATTEMPTED]
    ATTEMPTED.update(r[2] for r in rows)
    flags = []
    for directory, row in public_inputs.seed(rows):
        REPORT["inputs"].append(row)
        if directory is not None:
            flags.append("--distdir=" + str(directory))
    fixture.FLAGS.extend(flags)
    save()
    return flags


def run(name, args):
    args = list(args)
    for attempt in range(7):
        code, log = original_run(name, args)
        if not code:
            return code, log
        lines = log.read_text(errors="replace").splitlines()
        # Only recover an actual missing URL from the pinned public catalog.
        failed = "\n".join(line for line in lines if "Error downloading" in line)
        bep_flags = [arg.split("=", 1)[1] for arg in args if arg.startswith("--build_event_json_file=")]
        executed = any(Path(path).exists() and any("testResult" in json.loads(line).get("id", {}) for line in Path(path).read_text().splitlines() if line.strip()) for path in bep_flags)
        flags = [] if executed else admit(row for row in INPUTS if row[2] in failed)
        diagnostics = [line[:420] for line in lines if "ERROR:" in line or ".go:" in line][-6:]
        for index, line in enumerate(lines):
            if "Visibility error:" in line:
                diagnostics.extend(part[:420] for part in lines[index:index + 4])
                break
        REPORT.setdefault("diagnostics", {})[name] = diagnostics
        save()
        if not flags or attempt == 6:
            return code, log
        args[-1:-1] = flags


def record(name, func):
    row = REPORT["runs"][name] = {"verified": False}
    try:
        func(row)
    except (AssertionError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as err:
        row["error"] = type(err).__name__ + ": " + str(err)[:270]
        frame = traceback.extract_tb(err.__traceback__)[-1]
        row["error_location"] = f"{Path(frame.filename).name}:{frame.lineno}"
    save()


def cases(test, names):
    return {test, *(test + "/" + name for name in names)}


def guest(row):
    wanted = cases(fixture.COMPAT, ["wire", "graph"])
    log = fixture.run_test("guest", fixture.CMD, wanted, "^" + fixture.COMPAT + "$", row)
    for tag, key in (("CHECKPOINT_DUMP", "dumps"), ("CHECKPOINT_RESTORED", "restores"),
                     ("OLD_GUEST", "guests"), ("PRIVATE_COUNT", "private_controls")):
        row[key] = markers(log, tag)
    obs = row["namespace_observations"] = [json.loads(line.partition("CMD_GUEST_ORIGINAL ")[2])
                                          for line in log.splitlines() if line.startswith("CMD_GUEST_ORIGINAL ")]
    save()
    assert row["case_verdict"] and len(obs) == 1
    assert obs[0]["inner_exit"] == 0 and obs[0]["uname"] == "aarch64" and obs[0]["test_app"]["machine"] == 183 and obs[0]["sidecars"] == 7
    restores = {x["layout"]: x["image"] for x in row["restores"]}
    assert len(row["restores"]) == len(set(restores.values())) == 2 and set(restores) == {"wire", "graph"}
    dumps = row["dumps"]
    assert len(dumps) == 4 and {(x["layout"], x["mode"]) for x in dumps} == {(k, m) for k in restores for m in ("text", "html")}
    for x in dumps:
        assert x["image"] == restores[x["layout"]] and all(re.fullmatch("[0-9a-f]{64}", x[k]) for k in ("image", "expected", "rendered"))
        assert x["old"] in {"error", "wrong-boundaries", "match"} and int(x["regions"]) > 0 and int(x["files"]) >= 2
    guests = row["guests"]
    assert len(guests) == 2 and {x["layout"] for x in guests} == set(restores)
    assert all((x["pid"], x["files"], x["bytes_each"]) == ("1", "2", "8192") for x in guests)
    controls = row["private_controls"]
    assert len(controls) == 4 and {(x["layout"], x["control"]) for x in controls} == {(k, c) for k in restores for c in ("missing", "extra")}
    assert all(int(x["owners"]) > 0 and x["image"] == restores[x["layout"]] for x in controls)
    row["verified"] = True


def runners(row):
    family = "TestAutosaveLiveImages"
    log = fixture.run_test("autosave", "//test/runner:runner_test", cases(family, ["wire", "graph"]), "^" + family + "$", row)
    outputs = row["outputs"] = markers(log, "AUTOSAVE")
    save()
    assert row["case_verdict"] and len(outputs) == 4
    assert {(x["layout"], x["ext"]) for x in outputs} == {(k, e) for k in ("wire", "graph") for e in ("txt", "html")}
    assert all(re.fullmatch("[0-9a-f]{64}", x[key]) for x in outputs for key in ("image", "rendered"))
    row["verified"] = True


def main():
    assert platform.machine() == "aarch64" and fixture.git("rev-parse", "HEAD^") == PRODUCT
    for layout in ("wire", "graph"):
        directory = TRANSFER / layout
        directory.mkdir(parents=True)
        (directory / "checkpoint.img").touch(mode=0o600)
    fixture.run = run
    fixture.output_files = capture_outputs
    admit(row for row in INPUTS if row[0] in ("protobuf", "bazel_features", "rules_android", "bazel_skylib"))
    fixture.select(PUBLISHED)
    record("published_release", fixture.stage_old)
    fixture.select(CARRIER)
    fixture.FLAGS.append("--test_env=CHECKPOINT_TRANSFER=" + str(TRANSFER))
    if REPORT["runs"]["published_release"]["verified"]:
        record("guests", guest)
    media = {}
    for layout in ("wire", "graph"):
        path = TRANSFER / layout / "checkpoint.img"
        if path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= 128 << 20:
            media[layout] = fixture.H["digest"](path)
    REPORT["transferred_images"] = media
    save()
    if len(media) == 2:
        record("autosave", runners)
    producer = REPORT["runs"].get("guests", {})
    consumer = REPORT["runs"].get("autosave", {})
    old = REPORT["runs"]["published_release"].get("release", {}).get("gvisor-bin/gvisor_sentry", {}).get("sha256")
    if producer.get("verified") and consumer.get("verified"):
        new = producer["namespace_observations"][0]["current_sidecars"]["gvisor_sentry"]["sha256"]
        expected = {(x["layout"], x["mode"]): (x["image"], x["rendered"]) for x in producer["dumps"]}
        actual = {(x["layout"], "text" if x["ext"] == "txt" else "html"): (x["image"], x["rendered"])
                  for x in consumer["outputs"]}
        REPORT["verified"] = bool(old and old != new and expected == actual and
                                  all(media[x["layout"]] == x["image"] for x in producer["dumps"]))
    save()
    assert REPORT["verified"]


if __name__ == "__main__":
    main()
