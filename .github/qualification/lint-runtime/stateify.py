"""Compare stateify's seven production import-bearing outputs to their originals."""
import json
import os
import subprocess
import uuid

import check as harness

SOURCE = "250f1a64449754e811e33111090736ad6db8d727"
REFERENCE = {'//pkg/bpf:bpf_state_autogen': '2f5490c19126a6ba7a9485487d82cffe70c897a61491ccdbe1c9c30b672e765a', '//pkg/ebpf:ebpf_state_autogen': 'b64dc151e3f249de14d695aef9a2771dde66ae260113216139b60baa9542d196', '//pkg/sentry/fsimpl/overlay:overlay_state_autogen': 'f2fd5c7770c2752d934a45237e0c3e37f274c63e0f340d0fbd0458ef71910280', '//pkg/sentry/fsimpl/tmpfs:tmpfs_state_autogen': '49f19c4944ee35e7d9a2bafb9525729da48a33e10e7709c9b04f941b9bb48cb8', '//pkg/sentry/kernel:kernel_state_autogen': '9a2abe920ea105d328f39bfd39c1d996a2347061d1f8379a2c623d9f0d6c99ed', '//pkg/sentry/mm:mm_state_autogen': '8fba276068c77317e6886a792382b776e28dd8431aa124e9712fb50582551e54', '//pkg/sentry/socket/netstack:netstack_state_autogen': '53cfb9a61d7973a554ddcbf6fc47893cbbcfd0316a28f2eee661f9aa36900148'}
ARTIFACT = harness.ROOT / "stateify-production.json"
record = dict(source=SOURCE, started=harness.utc(), invocation=str(uuid.uuid4()),
              original_run=40956827445, expected=REFERENCE, actions={},
              completed={}, errors=[], verdict="INCOMPLETE")

def verify():
    harness.check(harness.git("rev-parse", "HEAD").decode().strip() == SOURCE, "wrong qualified source")
    bep = harness.ROOT / "production.bep"
    spawns = harness.ROOT / "production.exec"
    command = ["bazel", "--bazelrc=" + os.environ["QA_RC"], "build", "--config=x86_64",
               "--jobs=48", "--keep_going", "--extra_execution_platforms=//tools/bazeldefs:linux_amd64",
               "--spawn_strategy=local", "--strategy=GoStateify=remote",
               "--remote_default_exec_properties=Arch=amd64", "--remote_default_exec_properties=OSFamily=linux",
               "--remote_default_exec_properties=workload-isolation-type=oci",
               "--remote_default_exec_properties=container-image=" + harness.IMAGE,
               "--modify_execution_info=^GoStateify$=+no-remote-cache", "--invocation_id=" + record["invocation"],
               "--execution_log_json_file=" + str(spawns), "--build_event_json_file=" + str(bep), *REFERENCE]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    record["exit"] = result.returncode
    record["diagnostics"] = [s.strip()[:400] for s in result.stdout.splitlines() if (s.startswith("ERROR:") or ".go:" in s) and not any(k in s.lower() for k in ("header", "key=", "credential", "command line", "token"))][-20:]
    for row in harness.rows(bep):
        label = row.get("id",{}).get("targetCompleted",{}).get("label")
        if label in REFERENCE:
            record["completed"][label] = row.get("completed",{}).get("success",False)
    for spawn in harness.rows(spawns):
        label = spawn.get("targetLabel")
        if spawn.get("mnemonic") != "GoStateify" or label not in REFERENCE:
            continue
        outs = spawn.get("actualOutputs",[])
        props = {e["name"]:e["value"] for e in spawn["platform"]["properties"]}
        row = record["actions"][label] = dict(image=props.get("container-image"), runner=spawn["runner"],
                                             cached=spawn.get("cacheHit",False), exit=spawn.get("exitCode",0),
                                             digest=outs[0]["digest"]["hash"] if len(outs)==1 else None)
        if row != dict(image=harness.IMAGE, runner="remote", cached=False, exit=0, digest=REFERENCE[label]):
            record["errors"].append(f"action or bytes differ for {label}")
    harness.check(record["exit"] == 0 and record["completed"] == dict.fromkeys(REFERENCE, True), "missing or failed original target")
    harness.check(record["actions"].keys() == REFERENCE.keys() and not record["errors"], "missing action or byte comparison")
    record["verdict"] = "PASS"

try:
    verify()
except Exception as err:
    record["errors"].append(str(err))
    record["verdict"] = "FAIL"
finally:
    record["finished"] = harness.utc()
    ARTIFACT.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
print(json.dumps(record,sort_keys=True))
raise SystemExit(record["verdict"] != "PASS")
