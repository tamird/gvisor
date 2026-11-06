"""Build both public race binaries for AMD64 and ARM64 in one Bazel graph."""

import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from cross_race_elf import elf


work = Path(os.environ["RUNNER_TEMP"]) / "llvm-cross-race"
report = {"source": "f6df263f8e2ce2cbe6a497356613f6785c4800a4", "invocation": str(uuid4())}


def save():
    (work / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    save()
    targets = ["//:qualify_cross_runsc", "//:qualify_cross_sentry"]
    cmd = [
        work / "bazel", "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(work / "private.bazelrc"),
        "build", "--config=race", "-c", "opt", "--jobs=35", "--keep_going", "--strip=never",
        "--spawn_strategy=remote", "--noremote_local_fallback", "--remote_download_outputs=toplevel",
        "--extra_execution_platforms=//:qualify_linux_executor", "--host_platform=//:qualify_linux_executor",
        "--modify_execution_info=^GoLink$=+no-remote-cache", "--invocation_id=" + report["invocation"],
        "--build_event_json_file=" + str(work / "build.bep"),
        "--execution_log_compact_file=" + str(work / "build.compact"), "--color=no", "--noshow_progress", *targets,
    ]
    with (work / "build.log").open("wb") as log:
        report["exit"] = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
    report["completed"] = {}
    report["failed_actions"] = []
    bep = work / "build.bep"
    if bep.exists():
        with bep.open() as events:
            for line in events:
                event = json.loads(line)
                label = event["id"].get("targetCompleted", {}).get("label", "")
                if label in targets and "completed" in event and not event["id"]["targetCompleted"].get("aspect"):
                    report["completed"][label] = event["completed"].get("success", False)
                action = event.get("action", {})
                if action and not action.get("success") and len(report["failed_actions"]) < 12:
                    owner = event["id"].get("actionCompleted", {}).get("label", "")
                    if re.fullmatch(r"[@A-Za-z0-9+_./:-]{1,220}", owner):
                        report["failed_actions"].append([owner, action.get("type"), action.get("exitCode")])
    save()
    assert report["exit"] == 0 and report["completed"] == dict.fromkeys(targets, True), report
    report["outputs"] = {}
    for arch, machine in (("amd64", 62), ("arm64", 183)):
        for binary in ("runsc", "sentry"):
            name = f"{binary}-{arch}"
            info = elf(Path("bazel-bin/cross-race", name))
            report["outputs"][name] = info
            save()
            assert info["machine"] == machine and info["static"], info
            assert info["race_functions"] == ["runtime.racefini", "runtime.raceinit"], info

    # Read only the four relevant GoLink spawns. The raw execution log remains private.
    query = """select(.spawn) | .spawn | select(.mnemonic == "GoLink")
      | select(.targetLabel | test("^(@@?)?//(runsc:runsc-race|runsc/cmd/sentry:gvisor_sentry-race)(_impl)?$"))
      | [.targetLabel, .runner, (.cacheHit//false), (.exitCode//0),
         ([.platform.properties[]? | select(.name=="Arch") | .value] == ["amd64"])]"""
    with subprocess.Popen([work / "bb", "print", "--raw", "--compact_execution_log=" + str(work / "build.compact")],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as reader:
        rows = subprocess.run(["jq", "-c", query], stdin=reader.stdout, capture_output=True, check=True)
        reader.stdout.close()
        assert reader.wait() == 0
    links = report["links"] = [json.loads(line) for line in rows.stdout.splitlines()]
    save()
    assert len(links) == 4 and len({row[0] for row in links}) == 2, links
    assert all(row[1:] == ["remote", False, 0, True] for row in links), links
    report["verified"] = True
    save()


if __name__ == "__main__":
    main()
