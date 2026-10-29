"""Compare generated outputs and compiler behavior with the original formatter."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import check as harness

SOURCE = "cd92efa5f06a504a5f6bb187519f482d4941b21f"
PRODUCTION = {'//pkg/bpf:bpf_state_autogen': '2f5490c19126a6ba7a9485487d82cffe70c897a61491ccdbe1c9c30b672e765a', '//pkg/ebpf:ebpf_state_autogen': 'b64dc151e3f249de14d695aef9a2771dde66ae260113216139b60baa9542d196', '//pkg/sentry/fsimpl/overlay:overlay_state_autogen': 'f2fd5c7770c2752d934a45237e0c3e37f274c63e0f340d0fbd0458ef71910280', '//pkg/sentry/fsimpl/tmpfs:tmpfs_state_autogen': '49f19c4944ee35e7d9a2bafb9525729da48a33e10e7709c9b04f941b9bb48cb8', '//pkg/sentry/kernel:kernel_state_autogen': '9a2abe920ea105d328f39bfd39c1d996a2347061d1f8379a2c623d9f0d6c99ed', '//pkg/sentry/mm:mm_state_autogen': '8fba276068c77317e6886a792382b776e28dd8431aa124e9712fb50582551e54', '//pkg/sentry/socket/netstack:netstack_state_autogen': '53cfb9a61d7973a554ddcbf6fc47893cbbcfd0316a28f2eee661f9aa36900148'}
CASES = (
    "qualification_generated",
    "qualification_imports",
    "qualification_unaliased",
    "qualification_declared",
    "qualification_versioned",
)
record = dict(source=SOURCE, started=harness.utc(), invocation=str(uuid.uuid4()),
              original_run=40956827445, production=PRODUCTION, actions={},
              completed={}, errors=[], verdict="INCOMPLETE")
artifact = harness.ROOT / "stateify-comments.json"


def verify():
    harness.check(harness.git("rev-parse", "HEAD").decode().strip() == SOURCE, "wrong source")
    carrier = Path(__file__).resolve().parents[3]
    labels = {}
    cases = {}
    for name in CASES:
        pkg = Path("tools/go_stateify") / name
        shutil.copytree(carrier / pkg, pkg)
        label = f"//{pkg}:{name}"
        cases[label] = name
        labels[label + "_state_autogen"] = label
    expected = set(PRODUCTION) | cases.keys()
    record["expected"] = sorted(expected)
    bep = harness.ROOT / "stateify.bep"
    spawns = harness.ROOT / "stateify.exec"
    flags = ["bazel", "--bazelrc=" + os.environ["QA_RC"], "build", "--config=x86_64",
             "--jobs=48", "--keep_going", "--extra_execution_platforms=//tools/bazeldefs:linux_amd64",
             "--spawn_strategy=local", "--strategy=GoStateify=remote",
             "--remote_download_regex=.*qualification_[^/]*_state_autogen[.]go$",
             "--remote_default_exec_properties=Arch=amd64", "--remote_default_exec_properties=OSFamily=linux",
             "--remote_default_exec_properties=workload-isolation-type=oci",
             "--remote_default_exec_properties=container-image=" + harness.IMAGE,
             "--modify_execution_info=^GoStateify$=+no-remote-cache", "--invocation_id=" + record["invocation"],
             "--execution_log_json_file=" + str(spawns), "--build_event_json_file=" + str(bep)]
    result = subprocess.run(flags + sorted(expected), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    record["exit"] = result.returncode
    record["diagnostics"] = [s[:360] for s in result.stdout.splitlines() if ("undefined: " in s or s.startswith("ERROR:")) and not any(k in s.lower() for k in ("header", "token", "command line", "credential", "key="))][-22:]
    for row in harness.rows(bep):
        label = row.get("id", {}).get("targetCompleted", {}).get("label")
        if label in expected:
            record["completed"][label] = row.get("completed", {}).get("success", False)
    for spawn in harness.rows(spawns):
        label = spawn.get("targetLabel")
        if spawn.get("mnemonic") != "GoStateify" or label not in set(PRODUCTION) | labels.keys():
            continue
        outs = spawn.get("actualOutputs", [])
        image = dict((v["name"],v["value"]) for v in spawn["platform"]["properties"]).get("container-image")
        row = record["actions"][label] = dict(image=image, runner=spawn["runner"], cached=spawn.get("cacheHit", False), exit=spawn.get("exitCode",0))
        harness.check(image == harness.IMAGE and row["runner"] == "remote" and not row["cached"] and row["exit"] == 0 and len(outs)==1, f"incomplete scratch action: {label}")
        row["digest"] = outs[0]["digest"]["hash"]
        if label in PRODUCTION:
            harness.check(row["digest"] == PRODUCTION[label], f"production bytes changed: {label}")
        else:
            generated = Path(outs[0]["path"]).read_bytes()
            harness.check(harness.digest(generated)==row["digest"], f"download digest differs: {label}")
            row["imports"] = [s.strip() for s in generated.decode().splitlines() if '"math/rand/v2"' in s or '"net/netip"' in s or '"time"' in s]
    harness.check(set(record["completed"]) == expected, "missing targets")
    harness.check(set(record["actions"]) == set(PRODUCTION) | labels.keys(), "missing scratch actions")
    for label in PRODUCTION:
        harness.check(record["completed"][label], "production target missing: " + label)
    for label,name in cases.items():
        if name in ("qualification_generated","qualification_versioned"):
            # These have generated import-bearing files beside the output;
            # measure their behavior without presupposing sibling visibility.
            if not record["completed"][label]:
                harness.check(any(name in line and ("undefined: v2" in line or "undefined: peer" in line) for line in record["diagnostics"]), "unexpected generated sibling failure: " + label)
        elif name == "qualification_imports":
            harness.check(not record["completed"][label] and any(name in line and "undefined: v2" in line for line in record["diagnostics"]), "mixed checked-in arbitrary alias should reproduce stock failure")
        else:
            harness.check(record["completed"][label], "nonaliased case failed: " + label)
    harness.check(record["exit"]==1, "original compiler negative did not return 1")
    record["verdict"]="PASS_WITH_EXPECTED_CONTROL_FAILURE"

try:
    verify()
except Exception as err:
    record["errors"].append(str(err))
    record["verdict"]="FAIL"
finally:
    record["finished"]=harness.utc()
    artifact.write_text(json.dumps(record,indent=2,sort_keys=True)+"\n")
print(json.dumps(record,sort_keys=True))
raise SystemExit(record["verdict"] == "FAIL")
