"""Compare the release CLI and the actual autosave formatter on the same guest files."""

import json
import os
from pathlib import Path
import platform
import re
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
RECEIPT = WORK / "result.json"
INPUTS = json.loads((WORK / "inputs.json").read_text())
ATTEMPTED = set()
original_run = fixture.run


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
    save()


def cases(test, names):
    return {test, *(test + "/" + name for name in names)}


def check(name, owner, wanted, pattern, row):
    log = fixture.run_test(name, owner, wanted, pattern, row)
    assert row["case_verdict"], (name, row["cases"])
    return log


def guest(row):
    wanted = cases(fixture.COMPAT, ["wire", "graph"])
    log = check("guest", fixture.CMD, wanted, "^" + fixture.COMPAT + "$", row)
    obs, = [json.loads(line.split(" ", 1)[1]) for line in log.splitlines() if line.startswith("CMD_GUEST_ORIGINAL ")]
    assert obs["inner_exit"] == 0 and obs["uname"] == "aarch64" and obs["test_app"]["machine"] == 183
    assert obs["sidecars"] == 7
    row["namespace_observation"] = obs
    restores = re.findall(r"CHECKPOINT_RESTORED layout=(wire|graph) image=([0-9a-f]{64})", log)
    assert len(restores) == len(dict(restores)) == len(set(dict(restores).values())) == 2
    row["restores"] = dict(restores)
    assert sorted(re.findall(r"OLD_GUEST layout=(\w+) pid=1 files=2 bytes_each=8192", log)) == ["graph", "wire"]
    row["dumps"] = {}
    pattern = (r"CHECKPOINT_DUMP layout=(wire|graph) mode=(text|html) old=(error|wrong-boundaries|match) "
               r"image=([0-9a-f]{64}) regions=(\d+) files=(\d+) first=(-?\d+) old_first=(-?\d+) "
               r"old_regions=(\d+) expected=([0-9a-f]{64}) rendered=([0-9a-f]{64})")
    dumps = re.findall(pattern, log)
    assert len(dumps) == 4
    for layout, mode, old, image, regions, files, first, old_first, old_regions, expected, rendered in dumps:
        key = layout + "/" + mode
        assert key not in row["dumps"] and image == row["restores"][layout]
        assert old == "wrong-boundaries" and int(regions) > int(old_regions) > 0 and int(files) >= 2
        row["dumps"][key] = dict(image=image, regions=int(regions), stock_regions=int(old_regions),
                                 files=int(files), expected_region_sha256=expected, rendered=rendered)
    controls = re.findall(r"PRIVATE_COUNT layout=(wire|graph) control=(missing|extra) owners=(\d+) image=([0-9a-f]{64})", log)
    assert len(controls) == 4 and {(k,c) for k,c,_,_ in controls} == {(k,c) for k in ("wire","graph") for c in ("missing","extra")}
    assert all(int(n) > 0 and row["restores"][k] == image for k,_,n,image in controls)
    row["private_controls"] = controls
    row["verified"] = True


def runners(row):
    prior = REPORT["runs"]["guests"]
    assert prior["verified"]
    family = ("TestPrintCheckpointDir", "TestAutosaveLiveImages")
    wanted = set.union(*(cases(name, ["wire","graph"]) for name in family))
    log = check("autosave", "//test/runner:runner_test", wanted, "^(" + "|".join(family) + ")$", row)
    matches = re.findall(r"AUTOSAVE layout=(wire|graph) ext=(txt|html) image=([0-9a-f]{64}) rendered=([0-9a-f]{64})", log)
    assert len(matches) == 4
    row["outputs"] = {}
    for layout, ext, image, output in matches:
        key = layout + "/" + ("text" if ext == "txt" else "html")
        assert key not in row["outputs"]
        assert prior["restores"][layout] == image and prior["dumps"][key]["rendered"] == output
        row["outputs"][key] = dict(image=image, rendered=output)
    row["verified"] = True


def main():
    assert platform.machine() == "aarch64" and fixture.git("rev-parse", "HEAD^") == PRODUCT
    for layout in ("wire", "graph"):
        directory = TRANSFER / layout
        directory.mkdir(parents=True)
        (directory / "checkpoint.img").touch(mode=0o600)
    fixture.run = run
    admit(row for row in INPUTS if row[0] in ("protobuf", "bazel_features", "rules_android", "bazel_skylib"))
    fixture.select(PUBLISHED)
    record("published_release", fixture.stage_old)
    fixture.select(CARRIER)
    fixture.FLAGS.append("--test_env=CHECKPOINT_TRANSFER=" + str(TRANSFER))
    if REPORT["runs"]["published_release"]["verified"]:
        simple = "TestStatefileHTMLAndKeyVerification"
        def ordinary(row):
            check("generic", fixture.CMD, cases(simple, ["html_escapes_metadata_and_wire_strings", "key_enforced_for_metadata_and_dump_verify"]), "^" + simple + "$", row)
            row["verified"] = True
        record("generic_cli", ordinary)
        record("guests", guest)
    if REPORT["runs"].get("guests", {}).get("verified"):
        record("autosave", runners)
    old = REPORT["runs"]["published_release"].get("release", {}).get("gvisor-bin/gvisor_sentry", {}).get("sha256")
    new = REPORT["runs"].get("guests", {}).get("namespace_observation", {}).get("current_sidecars", {}).get("gvisor_sentry", {}).get("sha256")
    REPORT["verified"] = bool(old and new and old != new and len(REPORT["runs"]) == 4 and all(r["verified"] for r in REPORT["runs"].values()))
    save()
    assert REPORT["verified"]


if __name__ == "__main__":
    main()
