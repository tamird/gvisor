"""Compare Linux runtime link commands and their built outputs across source revisions."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

ROOT = Path.cwd()
APP = ROOT / "e2e/rules_cc"
TMP = Path(os.environ["RUNNER_TEMP"])
OUTPUT = TMP / "sidecar-evidence"
OUTPUT.mkdir(exist_ok=True)
BASE = "89c57ee6988962eca89daeb32e07813981729fe2"
STAGES = "baa2066bd9f6830d078228a9047649f8334f712e"
COMBINED = "7e7e7a794c4c735386362671939bfd4cf27c0a2d"
FIXED = "7b1a340bfc71c507113354cb363bfb5b19539ca5"
TESTS = ["//:freestanding_link_test", "//:cxx_shared_library_test"]
UNWIND = "@llvm//runtimes/libunwind:libunwind.shared"
CLI = ["bazel", f"--bazelrc={TMP / 'sidecar.bazelrc'}"]
REPORT = {}


def record():
    (OUTPUT / "report.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def run_bazel(tag, command, flags):
    stdout = TMP / f"{tag}.stdout"
    stderr = TMP / f"{tag}.stderr"
    with stdout.open("wb") as out, stderr.open("wb") as err:
        proc = subprocess.run([*CLI, command, *flags], cwd=APP, stdout=out, stderr=err)
    messages = []
    for line in stderr.read_text(errors="replace").splitlines():
        if any(x in line.lower() for x in ("error:", "unable to find library", "invocation id", "failed to build", "executed ", "processes:", "build completed")) and not any(x in line.lower() for x in ("header=", "api-key", "credential")):
            messages.append(line[-900:])
    if proc.returncode:
        lines = stderr.read_text(errors="replace").splitlines()
        start = next((i for i, line in enumerate(lines) if "ERROR:" in line), max(0, len(lines) - 60))
        saved = [line for line in lines[start:start + 100] if not any(token in line.lower() for token in ("header=", "api-key", "credential", "authorization:", "bearer "))]
        (OUTPUT / f"{tag}.stderr.txt").write_text("\n".join(saved) + "\n")
    result = {"exit": proc.returncode, "diagnostics": messages[-18:]}
    print(tag, json.dumps(result), flush=True)
    return stdout, result


def action_flags(tag, flags, targets=None):
    expression = 'mnemonic("CppLink", deps(set(' + " ".join(targets or [*TESTS, UNWIND]) + ')))'
    stdout, result = run_bazel(tag, "aquery", [*flags, "--include_param_files", "--noinclude_artifacts", "--output=jsonproto", expression])
    actions = []
    if result["exit"] == 0:
        graph = json.loads(stdout.read_bytes())
        labels = {t["id"]: t["label"] for t in graph["targets"]}
        (OUTPUT / f"{tag}.actions.json").write_text(json.dumps([
            {"label": labels[a["targetId"]], "arguments": a.get("arguments", []), "paramFiles": a.get("paramFiles", [])}
            for a in graph.get("actions", []) if labels[a["targetId"]].rsplit(":", 1)[-1].rsplit("/", 1)[-1] in ("libunwind.shared", "cxx_shared_library_test", "freestanding_link_test")
        ], indent=2) + "\n")
        for action in graph.get("actions", []):
            label = labels[action["targetId"]]
            name = label.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
            if name not in ("libunwind.shared", "cxx_shared_library_test", "freestanding_link_test"):
                continue
            args = action.get("arguments", []) + [v for p in action.get("paramFiles", []) for v in p.get("arguments", [])]
            row = {"name": name, "label": label, "pthread": "-lpthread" in args, "dl": "-ldl" in args, "search": [v for v in args if v.startswith("-L") and ("glibc" in v or "musl" in v)]}
            if row not in actions:
                actions.append(row)
    print(tag, "actions", json.dumps(actions), flush=True)
    result["actions"] = actions
    return result


def path_of(file):
    uri = urlparse(file.get("uri", ""))
    return Path(unquote(uri.path)) if uri.scheme == "file" else None


def build_and_test(tag, flags, is_gnu):
    bep = TMP / f"{tag}.bep"
    options = [*flags, "--jobs=60", "--remote_download_outputs=toplevel", "--keep_going", "--nobuild_event_json_file_path_conversion", f"--build_event_json_file={bep}"]
    if is_gnu:
        options += ["--strategy=TestRunner=local", "--cache_test_results=no", "--nobuild_tests_only", "--test_output=errors", *TESTS]
    _, result = run_bazel(tag, "test" if is_gnu else "build", [*options, UNWIND])
    tests = {}
    groups = {}
    outputs = []
    for line in bep.read_text().splitlines() if bep.exists() else []:
        event = json.loads(line)
        ids = event.get("id", {})
        if "namedSet" in ids:
            groups[ids["namedSet"]["id"]] = event["namedSetOfFiles"]
        if "targetCompleted" in ids and ids["targetCompleted"]["label"].endswith(":libunwind.shared"):
            outputs += [f["id"] for g in event.get("completed", {}).get("outputGroup", []) if g["name"] == "default" for f in g.get("fileSets", [])]
        if "testResult" in ids:
            label = ids["testResult"]["label"]
            dest = OUTPUT / (tag + "-" + label.split(":")[-1] + ".xml")
            for entry in event["testResult"].get("testActionOutput", []):
                xml = path_of(entry)
                if entry["name"] == "test.xml" and xml and xml.exists():
                    shutil.copyfile(xml, dest)
                    cases = ET.parse(xml).findall(".//testcase")
                    tests[label] = {"cases": len(cases), "failedOrSkipped": sum(any(c.find(k) is not None for k in ("skipped", "failure", "error")) for c in cases), "status": event["testResult"].get("status", "")}
    elfs = []
    seen = set()
    while outputs:
        group = outputs.pop()
        if group in seen:
            continue
        seen.add(group)
        for entry in groups[group].get("files", []):
            path = path_of(entry)
            if path and path.is_file() and path.read_bytes()[:4] == b"\x7fELF":
                dynamic = subprocess.check_output(["readelf", "-d", path], text=True)
                elfs.append({"name": entry["name"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "needed": [line.strip() for line in dynamic.splitlines() if "(NEEDED)" in line]})
        outputs += [v["id"] for v in groups[group].get("fileSets", [])]
    result["tests"] = tests
    result["elfs"] = elfs
    print(tag, "outputs", json.dumps({"tests": tests, "elfs": elfs}), flush=True)
    return result


def matches(row, name, enabled):
    actions = [a for a in row["args"]["actions"] if a["name"] == name]
    return bool(actions) and all(a["pthread"] == enabled and a["dl"] == enabled and a["search"] for a in actions)


def qualify(name, revision):
    subprocess.run(["git", "switch", "--quiet", "--detach", revision], check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    assert sha == revision
    result = REPORT[name] = {"revision": sha}
    for libc, platform in (("gnu", "linux_x86_64"), ("musl", "linux_x86_64_musl")):
        flags = [f"--platforms=@llvm//platforms:{platform}"]
        row = result[libc] = {}
        row["args"] = action_flags(f"{name}-{libc}-link", flags)
        record()
        row["actual"] = build_and_test(f"{name}-{libc}-build", flags, libc == "gnu")
        record()
    result["bootstrap_flags_preserved"] = all(matches(result[libc], "libunwind.shared", True) for libc in ("gnu", "musl"))
    result["complete_flags_preserved"] = all(matches(result[libc], "freestanding_link_test", False) and matches(result[libc], "cxx_shared_library_test", True) for libc in ("gnu", "musl"))
    result["built_both_unwinds"] = all(result[k]["actual"]["exit"] == 0 and result[k]["actual"]["elfs"] for k in ("gnu", "musl"))
    tests = result["gnu"]["actual"]["tests"]
    result["passed_original_tests"] = all(tests.get(t, {}).get("status") == "PASSED" and tests[t]["cases"] > 0 and tests[t]["failedOrSkipped"] == 0 for t in TESTS)
    result["pass"] = all(result[k] for k in ("bootstrap_flags_preserved", "complete_flags_preserved", "built_both_unwinds", "passed_original_tests"))
    record()
    return result["pass"]


os.environ["USE_BAZEL_VERSION"] = (ROOT / ".bazelversion").read_text().strip()
subprocess.run(["git", "fetch", "--no-tags", "--depth=1", "https://github.com/hermeticbuild/hermetic-llvm.git", BASE], check=True)
# The first job already built the baseline and attempted the combined builds.
# Its report contains their ELF digests and original test XML.
REPORT["first_run"] = "https://github.com/tamird/gvisor/actions/runs/43144267695/artifacts/13911485334"
for arm, revision in (("baseline", BASE), ("stages_only", STAGES), ("combined", COMBINED)):
    subprocess.run(["git", "switch", "--quiet", "--detach", revision], check=True)
    data = REPORT[arm] = {"revision": revision}
    # The freestanding fixture is introduced by #909. Keep the upstream #938
    # tree pristine and analyze its original hosted and unwind targets.
    targets = [TESTS[1], UNWIND] if arm == "stages_only" else [*TESTS, UNWIND]
    data["targets"] = targets
    for libc, platform in (("gnu", "linux_x86_64"), ("musl", "linux_x86_64_musl")):
        row = data[libc] = {"args": action_flags(f"{arm}-{libc}-link", [f"--platforms=@llvm//platforms:{platform}"], targets)}
        record()
fixed_pass = qualify("fixed", FIXED)
for arm in ("baseline", "stages_only"):
    for libc in ("gnu", "musl"):
        row = REPORT[arm][libc]
        assert row["args"]["exit"] == 0 and matches(row, "libunwind.shared", True), (arm, libc, row)
if not fixed_pass:
    raise SystemExit("The stage-specific correction did not restore every result.")
