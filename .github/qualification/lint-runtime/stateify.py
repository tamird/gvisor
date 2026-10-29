"""Compile existing stateify fixtures and compare their output on scratch."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import check as harness

SOURCE = "247a7b4fab5d1964f4915c3b492f207e2f8b4643"
CASES = {
    "qualification_unaliased": (["generated.go","record.go","values.go"], None),
    "qualification_generated": (["record.go", "values.go"], "d9a7cf2d466e3dee8f9503f726b6fb3891872ec08396f82d6c4765c2b4ae9e98"),
    "qualification_imports": (["generated.go", "record.go", "values.go"], "edd829e345569c74b7567380153b9d0edd2ab34c928b2f842c07ca4c0ebdad0f"),
}
OUT = {"source": SOURCE, "started": harness.utc(), "invocation": str(uuid.uuid4()), "expected": [], "actions": {}, "verdict": "INCOMPLETE", "errors": []}
ARTIFACT = harness.ROOT / "stateify-comments.json"

def persist():
    ARTIFACT.write_text(json.dumps(OUT, indent=2, sort_keys=True) + "\n")

def qualify():
    harness.check(harness.git("rev-parse", "HEAD").decode().strip() == SOURCE, "wrong product checkout")
    carrier = Path(__file__).resolve().parents[3]
    targets = []
    for name, (srcs, _) in CASES.items():
        package = Path("tools/go_stateify") / name
        shutil.copytree(carrier / package, package)
        targets.append(f"//{package}:{name}")
    generated = ["//pkg/sentry/" + owner + "_state_autogen" for owner in ("mm:mm","kernel:kernel","fsimpl/tmpfs:tmpfs","fsimpl/overlay:overlay","socket/netstack:netstack")]
    generated += ["//pkg/bpf:bpf_state_autogen","//pkg/ebpf:ebpf_state_autogen"]
    targets += generated
    OUT["expected"] = targets
    persist()
    exec_log = harness.ROOT / "stateify.exec"
    bep = harness.ROOT / "stateify.bep"
    command = ["bazel", "--bazelrc=" + os.environ["QA_RC"], "build", "--config=x86_64", "--jobs=48", "--keep_going",
               "--extra_execution_platforms=//tools/bazeldefs:linux_amd64",
               "--spawn_strategy=local", "--strategy=GoImports=local",
               "--remote_default_exec_properties=Arch=amd64", "--remote_default_exec_properties=OSFamily=linux",
               "--remote_default_exec_properties=workload-isolation-type=oci",
               "--remote_default_exec_properties=container-image=" + harness.IMAGE,
               "--modify_execution_info=^GoImports$=+no-cache", "--invocation_id=" + OUT["invocation"],
               "--execution_log_json_file=" + str(exec_log), "--build_event_json_file=" + str(bep), *targets]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    OUT["exit"] = result.returncode
    OUT["log_sha256"] = harness.digest(result.stdout.encode())
    OUT["diagnostics"] = [line[:500] for line in result.stdout.splitlines() if line.startswith(("ERROR:", "FAILED:")) and not any(block in line.lower() for block in ("key=", "header", "credential", "token", "command line"))][-15:]
    for event in harness.rows(bep):
        completed = event.get("id", {}).get("targetCompleted", {}).get("label")
        if completed in targets:
            OUT.setdefault("completed", {})[completed] = event.get("completed", {}).get("success", False)
    for spawn in harness.rows(exec_log):
        if spawn.get("mnemonic") != "GoImports":
            continue
        label = spawn["targetLabel"]
        outputs = spawn.get("actualOutputs", [])
        if len(outputs) != 1:
            OUT["errors"].append(f"{label}: exit {spawn.get('exitCode', 0)}, {len(outputs)} outputs")
            continue
        output = outputs[0]
        if "/qualification_" in label or any(t in label for t in generated):
            OUT["actions"][label] = {"runner": spawn.get("runner"), "exit": spawn.get("exitCode",0), "digest": output["digest"]["hash"]}
    for name in CASES:
        label = f"//tools/go_stateify/{name}:{name}"
        harness.check(label in OUT.get("completed",{}), "missing fixture result " + label)
    harness.check(all(OUT.get("completed",{}).get(t) for t in generated), "one or more actual generator targets failed")
    harness.check(all(any(name in label for label in OUT["actions"]) for name in CASES), "missing GoImports action")
    # Preserve all actual build failures, with enough diagnostics to distinguish
    # a missing qualifier from a worker or analyzer failure.
    OUT["compiler_diagnostics"]=[line[:350] for line in result.stdout.splitlines() if "undefined:" in line or " imported and not used" in line][:25]
    harness.check(result.returncode == 0, "Bazel build returned " + str(result.returncode))
    OUT["verdict"] = "PASS"

try:
    qualify()
except Exception as exc:
    OUT["errors"].append(str(exc))
    OUT["verdict"] = "FAIL"
finally:
    OUT["finished"] = harness.utc()
    persist()
print(json.dumps(OUT, sort_keys=True))
raise SystemExit(OUT["verdict"] != "PASS")
