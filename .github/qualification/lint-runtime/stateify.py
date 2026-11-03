"""Compare the existing stateify import contracts with a native Bazel launcher."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid
import zipfile

import check as harness

SOURCE = "531ca4ff692b6eb7383e01ee25b0a8ad607a90ec"
CONSUMER = "7cbb007c3febee15d1904c7faaf3bb21f34a3ae5"
SUPPLIER = "6b4da63029e77fc08377ef9b7e0181fec5c7aad5"
PATCH_SHA256 = "d31d9da040c28fb79fdf2b4e0edbccd74ef5bb6bf4fe1f36912a59edd5708355"
BASELINE_SHA256 = "e6e2ce3880e23b738c9243df47cda99a37c23898aae53ab67fabf4ca43d75fd5"
BASE = "c3d3fd4a3844b46529b6d15216b3410b872df735"
FIXTURES = "a99f9ab1602f404a40ae2c8af81ad820b47ffc5c"
PRODUCTION = (
    "//pkg/bpf:bpf_state_autogen",
    "//pkg/ebpf:ebpf_state_autogen",
    "//pkg/sentry/fsimpl/overlay:overlay_state_autogen",
    "//pkg/sentry/fsimpl/tmpfs:tmpfs_state_autogen",
    "//pkg/sentry/kernel:kernel_state_autogen",
    "//pkg/sentry/mm:mm_state_autogen",
    "//pkg/sentry/socket/netstack:netstack_state_autogen",
)
CASES = ("qualification_generated", "qualification_imports", "qualification_unaliased",
         "qualification_declared", "qualification_versioned")
CASE_LABELS = {f"//tools/go_stateify/{name}:{name}": name for name in CASES}
FORMATS = set(PRODUCTION) | {label + "_state_autogen" for label in CASE_LABELS}
EXPECTED = set(PRODUCTION) | CASE_LABELS.keys()
ROOT = harness.ROOT
REPORT = ROOT / "stateify-native-review.json"
OUT = {"source": SOURCE, "consumer": CONSUMER, "supplier": SUPPLIER, "base": BASE, "fixtures": FIXTURES, "phases": {}, "verdict": "INCOMPLETE"}


def save():
    REPORT.write_text(json.dumps(OUT, indent=2, sort_keys=True) + "\n")


def phase(name, fn):
    row = OUT["phases"][name] = {"verdict": "INCOMPLETE", "started": harness.utc()}
    try:
        fn(row)
        row["verdict"] = "PASS"
    except Exception as exc:
        row.update(verdict="FAIL", error=str(exc))
    finally:
        row["verified_at"] = harness.utc()
        save()


def build(row, name, source, minimal, negative=False):
    subprocess.run(["git", "checkout", "--detach", source], check=True, stdout=subprocess.DEVNULL)
    row["source"] = harness.git("rev-parse", "HEAD").decode().strip()
    harness.check(row["source"] == harness.git("rev-parse", source).decode().strip(), "wrong checkout")
    harness.check(not harness.git("diff", "--name-only"), "modified tracked source")
    image = harness.IMAGE if minimal else harness.GNU_IMAGE
    row.update(image=image, invocation=str(uuid.uuid4()), expected=sorted(EXPECTED))
    bep = ROOT / f"stateify-{name}.bep"
    spawns = ROOT / f"stateify-{name}.exec"
    labels = [PRODUCTION[0]] if negative else sorted(EXPECTED)
    command = ["bazel", "--bazelrc=" + os.environ["QA_RC"], "build", "--config=x86_64", "--jobs=16",
               "--keep_going", "--extra_execution_platforms=//tools/bazeldefs:linux_amd64", "--spawn_strategy=local", "--strategy=GoStateify=remote", "--strategy=GoImports=remote",
               "--remote_default_exec_properties=Arch=amd64", "--remote_default_exec_properties=OSFamily=linux",
               "--remote_default_exec_properties=workload-isolation-type=" + ("oci" if minimal else "firecracker"),
               "--remote_default_exec_properties=recycle-runner=false", "--remote_default_exec_properties=container-image=" + image,
               "--remote_download_regex=.*_state_autogen.*[.]go$", "--modify_execution_info=^GoImports$=+no-cache",
               "--invocation_id=" + row["invocation"], "--build_event_json_file=" + str(bep),
               "--execution_log_json_file=" + str(spawns)] + labels
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    row.update(exit=proc.returncode, finished=harness.utc(), log_sha256=harness.digest(proc.stdout))
    log = proc.stdout.decode(errors="replace")
    row["diagnostics"] = [line[:600] for line in log.splitlines()
                          if any(s in line for s in ("undefined: ", "ERROR:", "no such file or directory", "No such file or directory"))
                          and not any(s in line.lower() for s in ("token", "credential", "api-key", "authorization"))][:40]
    save()
    row["completed"] = {event["id"]["targetCompleted"]["label"]: event["completed"].get("success", False)
                        for event in harness.rows(bep)
                        if event.get("id", {}).get("targetCompleted", {}).get("label") in labels
                        and not event["id"]["targetCompleted"].get("aspect")}
    selected = [spawn for spawn in harness.rows(spawns)
                if spawn.get("mnemonic") == "GoImports" and spawn.get("targetLabel") in FORMATS]
    row["actions"] = {}
    for spawn in selected:
        label = spawn["targetLabel"]
        harness.check(label not in row["actions"], "duplicate formatter owner")
        props = {p["name"]: p["value"] for p in spawn.get("platform", {}).get("properties", [])}
        action = row["actions"][label] = {"runner": spawn["runner"], "cached": spawn.get("cacheHit", False),
                                         "exit": spawn.get("exitCode", 0), "platform": props,
                                         "command": spawn["commandArgs"], "outputs": spawn.get("actualOutputs", [])}
        harness.check(props.get("container-image") == image and props.get("Arch") == "amd64"
                      and props.get("OSFamily") == "linux" and props.get("recycle-runner") == "false"
                      and props.get("workload-isolation-type") == ("oci" if minimal else "firecracker"), "wrong worker")
        harness.check(action["runner"] == "remote" and not action["cached"], "formatter did not execute remotely")
        if not negative:
            output, = action["outputs"]
            command = action["command"]
            harness.check(len(command) == 7 and command[1] == "runwithsdk"
                          and Path(command[2]).name == "go" and command[4] == output["path"]
                          and Path(command[5]).name == "goimports" and command[6].endswith(".go"),
                          f"unexpected SDK launcher invocation for {label}: {command}")
            generated = Path(output["path"]).read_bytes()
            harness.check(action["exit"] == 0 and harness.digest(generated) == output["digest"]["hash"], "wrong formatted output")
            action["digest"] = output["digest"]["hash"]
            action["imports"] = [s.strip() for s in generated.decode().splitlines()
                                 if any(p in s for p in ('"math/rand/v2"', '"net/netip"', '"time"'))]
    if negative:
        harness.check(proc.returncode != 0 and row["completed"] == {PRODUCTION[0]: False}, "stock scratch unexpectedly passed")
        action, = row["actions"].values()
        harness.check(action["exit"] != 0 and action["command"][0].endswith("/goimports.sh"), "wrong failed launcher")
        harness.check(any("no such file or directory" in line.lower() for line in row["diagnostics"]), "missing launcher failure")
        execroot = subprocess.check_output(["bazel", "--bazelrc=" + os.environ["QA_RC"], "info", "execution_root"]).decode().strip()
        script = (Path(execroot) / action["command"][0]).read_bytes()
        inputs = {item["path"]: item.get("digest", {}) for item in selected[0].get("inputs", [])}
        action["launcher_sha256"] = harness.digest(script)
        action["shebang"] = script.splitlines()[0].decode()
        harness.check(action["shebang"] == "#!/bin/sh" and inputs[action["command"][0]]["hash"] == action["launcher_sha256"],
                      "missing actual shell-launcher identity")
        return
    harness.check(proc.returncode == 1 and set(row["completed"]) == EXPECTED and set(row["actions"]) == FORMATS, "missing expected owners")
    harness.check(all(row["completed"][label] for label in PRODUCTION), "production generation failed")
    for label, name in CASE_LABELS.items():
        if name in ("qualification_declared", "qualification_unaliased"):
            harness.check(row["completed"][label], "previously passing import layout failed: " + name)
        elif not row["completed"][label]:
            harness.check(any(name in line and ("undefined: v2" in line or "undefined: peer" in line)
                              for line in row["diagnostics"]), "unexpected compile failure: " + name)
    harness.check(not row["completed"][next(label for label, name in CASE_LABELS.items() if name == "qualification_imports")],
                  "arbitrary-alias negative did not reproduce")


def compare(row):
    before = OUT["phases"]["stock_gnu"]
    after = OUT["phases"]["native_minimal"]
    harness.check(before["verdict"] == after["verdict"] == "PASS", "missing independent positive")
    row["same_dispositions"] = before["completed"] == after["completed"]
    row["same_formatted_bytes"] = {label: before["actions"][label]["digest"] == after["actions"][label]["digest"]
                                    for label in sorted(FORMATS)}
    harness.check(row["same_dispositions"] and all(row["same_formatted_bytes"].values()), "caller behavior changed")


OUT["started"] = harness.utc()
carrier = Path(os.environ["CARRIER_DIR"])
for name in CASES:
    path = Path("tools/go_stateify") / name
    shutil.copytree(carrier / path, path)
OUT["fixture_files"] = {str(path.relative_to(carrier)): harness.digest(path.read_bytes())
                        for name in CASES for path in (carrier / "tools/go_stateify" / name).iterdir()}
harness.check(OUT["fixture_files"] == {'tools/go_stateify/qualification_unaliased/values.go': '441ac0c3219494bfe446533618a2f70fcae8b38c047379ad9fb3216f753b6458', 'tools/go_stateify/qualification_unaliased/record.go': 'f0a945e413098908118d43bf78f702a6245d75f32a0bdf8917b63ebbb3673afc', 'tools/go_stateify/qualification_unaliased/BUILD': '5e593107c3b09c780bc5db25eee873b3c1af82760ba215e62183c1b8435d8c71', 'tools/go_stateify/qualification_generated/BUILD': '712d59e51b56adac97b94cffba7e2babcafa3333819b26f524a745c165ba87f0', 'tools/go_stateify/qualification_declared/values.go': 'b334f3b4ae235c277acb5f8467eb56ff77e54990627b4c4428bba00dc1836164', 'tools/go_stateify/qualification_declared/record.go': '77ae1a6857bbccffe0a796de9392fd4a5490e92023c192482f569397b0a077ce', 'tools/go_stateify/qualification_declared/BUILD': '8bdd01873ab742c07ad46a6f351fc66a9cb4bf24ff654f79c8bd3500547b4239', 'tools/go_stateify/qualification_imports/values.go': '4f9aa9a4c41b171c42a91219a0b0134987edc6940d5371f21bba318bb73cff07', 'tools/go_stateify/qualification_imports/record.go': '1d9e0f9c742f9d5a8ac9012e8dd3d4d0199398717e8740b651383d236c226855', 'tools/go_stateify/qualification_imports/BUILD': '825a8331b767bc105484c07453c8545ee5a669f46017ef9ada0bdaeb53907798', 'tools/go_stateify/qualification_versioned/record.go': 'a2322508c0891cd3452126754106a744b45d910bdb157930d75c437da3c72a48', 'tools/go_stateify/qualification_versioned/BUILD': 'fd9b9fd928b1941299024f74833ba38838d7ad20a3b18f410c88267a37bcd880'}, "fixture bytes changed")
save()
def load_controls(row):
    archive = ROOT / "stateify-control.zip"
    harness.check(harness.digest(archive.read_bytes()) == BASELINE_SHA256, "baseline ZIP digest differs")
    with zipfile.ZipFile(archive) as bundle:
        retained = json.loads(bundle.read("stateify-native-review.json"))
    harness.check(retained["verdict"] == "PASS" and retained["base"] == BASE
                  and retained["fixture_files"] == OUT["fixture_files"], "incompatible control evidence")
    for name in ("stock_gnu", "stock_minimal"):
        control = retained["phases"][name]
        harness.check(control["verdict"] == "PASS" and control["source"] == BASE,
                      f"{name} lacks a verified result for {BASE}")
        OUT["phases"][name] = control
    harness.check(harness.git("rev-parse", SOURCE + "^").decode().strip() == CONSUMER,
                  "integration commit has the wrong consumer parent")
    patch = Path("tools/rules_go_sdk_action.patch")
    harness.check(harness.digest(patch.read_bytes()) == PATCH_SHA256, "supplier patch digest differs")
    row.update(artifact=13200360924, sha256=BASELINE_SHA256, supplier_patch_sha256=PATCH_SHA256)


phase("retained_controls", load_controls)
if OUT["phases"]["retained_controls"]["verdict"] == "PASS":
    phase("native_minimal", lambda row: build(row, "candidate", SOURCE, True))
    phase("comparison", compare)
OUT["verdict"] = "PASS" if all(row["verdict"] == "PASS" for row in OUT["phases"].values()) else "FAIL"
OUT["finished"] = harness.utc()
save()
raise SystemExit(OUT["verdict"] != "PASS")
