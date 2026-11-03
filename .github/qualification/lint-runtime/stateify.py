"""Verify the published release, declared inputs and import binding controls."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
import zipfile

import check as harness

SOURCE = "d4905394df68e484d04658d2ec22eaa2143521cd"
BASE = "471d6dff5229b99208bf6fec985601067f1f9d69"
EXPORT = "1899f71d79be9b78ed439313ee050ff6b8d62a5d"
STOCK_ZIP = "5161da9ab19357a8434690839579212cddeb6179635a23cd2766383b5f72415e"
ROOT = harness.ROOT
REPORT = ROOT / "stateify-native-review.json"
OUT = {"source": SOURCE, "base": BASE, "export": EXPORT, "started": harness.utc(),
       "verdict": "INCOMPLETE", "phases": {}}
OLD = ("generated", "imports", "unaliased", "declared", "versioned")
EXTRA = ("odd_implicit", "odd_declared", "odd_named", "odd_aliased", "generic", "literal", "ambiguous")
EXPECTED_BAD = {"odd_implicit", "odd_declared", "ambiguous"}
OLD_PRODUCTION = ("//pkg/bpf:bpf", "//pkg/ebpf:ebpf", "//pkg/sentry/fsimpl/overlay:overlay",
                  "//pkg/sentry/fsimpl/tmpfs:tmpfs", "//pkg/sentry/kernel:kernel", "//pkg/sentry/mm:mm",
                  "//pkg/sentry/socket/netstack:netstack")
check = harness.check


def save():
    REPORT.write_text(json.dumps(OUT, indent=2, sort_keys=True) + "\n")


def phase(name, fn):
    row = OUT["phases"][name] = {"verdict": "INCOMPLETE", "started": harness.utc()}
    save()
    try:
        fn(row)
        row["verdict"] = "PASS"
    except Exception as exc:
        row["verdict"] = "FAIL"
        row["exception"] = type(exc).__name__
        if isinstance(exc, (AssertionError, ValueError)):
            row["reason"] = str(exc)[:200]
    finally:
        row["verified_at"] = harness.utc()
        save()


def label(short):
    name = "qualification_" + short
    return "//tools/go_stateify/" + name + ":" + name


def setup(row):
    carrier = Path(os.environ["CARRIER_DIR"]) / "tools/go_stateify"
    for path in sorted(carrier.glob("qualification_*")):
        shutil.copytree(path, Path("tools/go_stateify") / path.name)
    files = {str(p.relative_to(carrier.parent.parent)): harness.digest(p.read_bytes())
             for p in carrier.rglob("*") if p.is_file()}
    archive = ROOT / "stateify-control.zip"
    check(harness.digest(archive.read_bytes()) == STOCK_ZIP, "wrong retained ZIP")
    with zipfile.ZipFile(archive) as bundle:
        retained = json.loads(bundle.read("stateify-native-review.json"))
    check(retained["verdict"] == "PASS" and retained["base"] == BASE
          and retained["phases"]["stock_gnu"]["source"] == BASE, "incompatible stock")
    check(all(files.get(p) == digest for p, digest in retained["fixture_files"].items()), "old fixture changed")
    row.update(stock_artifact=13251728088, zip_sha256=STOCK_ZIP, fixture_hashes=files,
               gnu=retained["phases"]["stock_gnu"]["completed"],
               stock_outputs={label: a["digest"] for label, a in retained["phases"]["stock_gnu"]["actions"].items()})
    check(harness.git("rev-parse", SOURCE+"^").decode().strip() == BASE, "wrong base")
    check("471d6dff5" in harness.git("show", "-s", "--format=%s", EXPORT).decode(), "wrong Go release")
    check(len(row["stock_outputs"]) == 12 and len(row["gnu"]) == 12, "missing stock results")


def build(row, phase_name, source, labels, *, minimal, focused=False):
    subprocess.run(["git", "checkout", "--detach", source], check=True, stdout=subprocess.DEVNULL)
    check(harness.git("rev-parse", "HEAD").decode().strip() == source and not harness.git("diff", "--name-only"),
          "incorrect product source")
    bep = ROOT / (phase_name + ".bep")
    spawns = ROOT / (phase_name + ".exec")
    row.update(source=source, invocation=str(uuid.uuid4()), expected=len(labels))
    image = harness.IMAGE if minimal else harness.GNU_IMAGE
    args = ["bazel", "--bazelrc=" + os.environ["QA_RC"], "build", "--config=x86_64", "--jobs=16", "--keep_going",
            "--extra_execution_platforms=//tools/bazeldefs:linux_amd64", "--spawn_strategy=local",
            "--strategy=GoStateify=remote", "--strategy=GoImports=remote", "--remote_default_exec_properties=Arch=amd64",
            "--remote_default_exec_properties=OSFamily=linux", "--remote_default_exec_properties=recycle-runner=false",
            "--remote_default_exec_properties=workload-isolation-type=" + ("oci" if minimal else "firecracker"),
            "--remote_default_exec_properties=container-image=" + image, "--remote_download_regex=.*_state_autogen.*[.]go$",
            "--invocation_id="+row["invocation"], "--build_event_json_file="+str(bep)]
    if focused:
        args.extend(["--modify_execution_info=^GoStateify$=+no-cache", "--execution_log_json_file="+str(spawns)])
    cmd = subprocess.run(args + sorted(labels), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    row.update(exit=cmd.returncode, finished=harness.utc(), log_sha256=harness.digest(cmd.stdout))
    log = cmd.stdout.decode(errors="replace")
    row["diagnostics"] = [line[:400] for line in log.splitlines()
                          if re.search(r"undefined:|no import for|generated code uses|no such target|no such package|syntax error|not used|invalid import path|cannot use|cannot find package", line)
                          and not re.search(r"(?i)(token|key=|authorization|credential|://)", line)][:30]
    row["completed"] = {event["id"]["targetCompleted"]["label"]: event["completed"].get("success", False)
                        for event in harness.rows(bep) if "targetCompleted" in event.get("id", {})}
    save()
    if focused:
        actions = row["generator_actions"] = {}
        for spawn in harness.rows(spawns):
            check(spawn.get("mnemonic") != "GoImports", "formatter executed")
            if spawn.get("mnemonic") != "GoStateify" or spawn.get("targetLabel") not in ({l+"_state_autogen" for l in labels} | labels):
                continue
            target = spawn["targetLabel"]
            command = spawn["commandArgs"]
            props = {p["name"]: p["value"] for p in spawn.get("platform", {}).get("properties", [])}
            outputs = spawn.get("actualOutputs", [])
            row2 = actions[target] = {"runner": spawn.get("runner"), "cached": spawn.get("cacheHit", False),
                                      "exit": spawn.get("exitCode", 0), "tool": command[0], "image": props.get("container-image")}
            check(row2["runner"] == "remote" and not row2["cached"] and props.get("container-image") == image
                  and props.get("workload-isolation-type") == "oci" and Path(command[0]).name == "stateify",
                  "generator did not run directly on shell-free worker")
            if row2["exit"] == 0:
                generated, = outputs
                content = Path(generated["path"]).read_bytes()
                row2["digest"] = harness.digest(content)
                check(row2["digest"] == generated["digest"]["hash"], "wrong materialized generator output")
        save()
    return log


def observation(row):
    ids = [label(k) for k in ("odd_implicit", "odd_declared", "odd_aliased", "literal", "generic")]
    build(row, "stock_shape", BASE, ids, minimal=False)
    check(set(row["completed"]) == set(ids), "stock observation incomplete")
    check(row["exit"] == (0 if all(row["completed"].values()) else 1), "stock observation lost its native disposition")


def fixtures(row):
    production = {p+"_state_autogen" for p in OLD_PRODUCTION}
    libs = {label(k) for k in OLD + EXTRA}
    log = build(row, "candidate", SOURCE, production | libs, minimal=True, focused=True)
    check(set(row["completed"]) == production | libs and row["exit"] == 1, "candidate owners missing")
    bad = {label(k) for k in EXPECTED_BAD}
    check({l for l, v in row["completed"].items() if not v} == bad, "unexpected candidate dispositions")
    check("generated code uses \"peer\" for both" in log and "no import for \"shapes\"" in log, "wrong negative")
    actions = row["generator_actions"]
    expected = production | {l+"_state_autogen" for l in libs}
    check(expected == set(actions), "missing or unexpected generator action")
    before = OUT["phases"]["retained"]["stock_outputs"]
    identical = production | {label(k)+"_state_autogen" for k in ("declared", "unaliased")}
    check(all(actions[l]["digest"] == before[l] for l in identical), "previously correct output changed")
    row["unchanged_from_stock"] = sorted(identical)
    old = OUT["phases"]["stock_unusual_names"].get("completed", {})
    row["stock_name_regressions"] = [l for l, passed in old.items() if passed and not row["completed"].get(l)]
    check(old and not row["stock_name_regressions"], "an unaliased name accepted by stock needs compatibility")


def all_outputs(row):
    paths = [s for s in harness.git("ls-tree", "-r", "--name-only", EXPORT).decode().splitlines()
             if s.endswith("_state_autogen.go")]
    check(len(paths) == 485, "wrong exported census")
    targets = {"//"+str(Path(p).parent)+":"+Path(p).stem: p for p in paths}
    build(row, "release", SOURCE, targets, minimal=True)
    rows = row["files"] = {}
    for target, path in targets.items():
        content = harness.git("show", EXPORT+":"+path)
        check(b"stateify" in content.splitlines()[0], "wrong reference generator")
        actual = Path("bazel-bin") / path
        rows[target] = {"reference": harness.digest(content), "generated": harness.digest(actual.read_bytes())
                        if row["completed"].get(target) and actual.is_file() else None}
    row["exact"] = sum(d["generated"] == d["reference"] for d in rows.values())
    row["missing"] = [l for l, d in rows.items() if d["generated"] is None]
    row["changed"] = [l for l, d in rows.items() if d["generated"] and d["generated"] != d["reference"]]
    save()
    check(row["exit"] == 0 and set(row["completed"]) == set(targets) and row["exact"] == 485,
          "exported bytes or coverage differs")


phase("retained", setup)
if OUT["phases"]["retained"]["verdict"] == "PASS":
    phase("stock_unusual_names", observation)
    phase("candidate_imports", fixtures)
    phase("exported_tree", all_outputs)
OUT["verdict"] = "PASS" if all(p["verdict"] == "PASS" for p in OUT["phases"].values()) else "FAIL"
OUT["finished"] = harness.utc()
save()
raise SystemExit(OUT["verdict"] != "PASS")
