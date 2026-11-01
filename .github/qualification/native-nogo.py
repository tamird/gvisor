"""Run Buildkite's complete Nogo command and report configured vs executed tests."""

import collections
import json
import os
import platform
import shlex
import shutil
import subprocess
import time
from pathlib import Path

root = Path(os.environ["RUNNER_TEMP"])
out = root / "nogo-receipts"
events = Path(".build_events.json")
console = root / "native-nogo.console"


def write(name, value):
    (out / (name + ".json")).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def commands():
    result = []
    for pipeline in ("first-pass", "pipeline"):
        lines = Path(f".buildkite/{pipeline}.yaml").read_text().split('    label: ":golang: Nogo tests"\n')[1].splitlines()
        result.append(shlex.split(lines[0].removeprefix("    command: ")))
        assert lines[1:3] == ["    env:", '      DOCKER_BUILD: "false"']
    assert result[0] == result[1] == ["bazel", "test", "--config=buildkite", "--config=nogo", "//..."]
    return [result[0][0], f"--bazelrc={root}/nogo-resources.bazelrc", *result[0][1:]]


def valid_log_line(line):
    return not any(k in line.lower() for k in ("http", "token", "api-key", "header=", "bearer", "grpcs:"))


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == os.environ["PRODUCT"]
    cmd = commands()
    started = int(time.time())
    free_start = shutil.disk_usage(".").free
    with console.open("w") as f:
        status = subprocess.run(["timeout", "--kill-after=30s", "110m", *cmd], stdout=f, stderr=f).returncode
    finished = int(time.time())
    configured = set()
    complete = {}
    summaries = {}
    results = collections.defaultdict(list)
    actions = []
    bazel_exit = None
    event_tail_lost = False
    if events.is_file():
        with events.open() as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    event_tail_lost = True
                    continue
                ident = ev["id"]
                if "targetConfigured" in ident and not ident["targetConfigured"].get("aspect"):
                    configured.add(ident["targetConfigured"]["label"])
                if "targetCompleted" in ident and not ident["targetCompleted"].get("aspect"):
                    complete[ident["targetCompleted"]["label"]] = ev.get("completed", {}).get("success", False)
                if "testSummary" in ev:
                    summaries[ident["testSummary"]["label"]] = ev["testSummary"]["overallStatus"]
                if "testResult" in ev:
                    t = ev["testResult"]
                    results[ident["testResult"]["label"]].append({"status": t.get("status"), "cachedLocally": t.get("cachedLocally", False), "strategy": t.get("executionInfo", {}).get("strategy")})
                if "action" in ev and not ev["action"].get("success", False) and len(actions) < 50:
                    a = ev["action"]
                    actions.append({"label":ident.get("actionCompleted",{}).get("label"), "mnemonic":a.get("type"), "exit":a.get("exitCode")})
                if "finished" in ev:
                    bazel_exit = ev["finished"]["exitCode"]
    markers = {}
    failures = {}
    for label in sorted(results):
        if not label.startswith("//"):
            continue
        p = Path("bazel-testlogs") / label[2:].replace(":", "/") / "test.log"
        if p.is_file():
            s = p.read_text(errors="replace")
            markers[label] = {a: a+"..." in s for a in ("amd64", "arm64")}
            if summaries.get(label) != "PASSED":
                failures[label] = [l[:400] for l in s.splitlines() if valid_log_line(l)][:18]
    errs = []
    tail = collections.deque(maxlen=6)
    with console.open(errors="replace") as f:
        for line in f:
            if valid_log_line(line):
                if ("ERROR:" in line or "not found" in line or "No such file" in line or "No space" in line) and len(errs)<45:
                    errs.append(line.rstrip()[:650])
                if line.strip():
                    tail.append(line.rstrip()[:350])
    sets_agree = configured == set(results) == set(summaries)
    log_arms = all(m.get(a) for m in markers.values() for a in ("amd64","arm64"))
    known = {"//pkg/sentry/loader/vdsodata:vdsodata_nogo","//pkg/sentry/platform/systrap/sysmsg:sysmsg_nogo","//pkg/bits:bits_nogo","//pkg/tcpip/seqnum:seqnum_nogo"}
    ok = status == 0 and bazel_exit and bazel_exit["name"] == "SUCCESS" and sets_agree and known <= configured and set(markers)==configured and log_arms and all(v=="PASSED" for v in summaries.values())
    write("owners", {"configured": sorted(configured), "completed": complete, "summaries":summaries, "results":results, "markers":markers, "failedLogs":failures})
    receipt = {"verified":bool(ok),"source":os.environ["PRODUCT"],"host":platform.machine(),"aarch64_gcc":shutil.which("aarch64-linux-gnu-gcc"),"bazel_command":cmd[2:],"bazel_exit":bazel_exit,"status":status,"started":started,"finished":finished,"filed":int(time.time()),"diskFreeBefore":free_start,"diskFreeAfter":shutil.disk_usage(".").free,"configured":len(configured),"summaries":dict(collections.Counter(summaries.values())),"testRunners":len(results),"amd64Markers":sum(m["amd64"] for m in markers.values()),"arm64Markers":sum(m["arm64"] for m in markers.values()),"eventTailLost":event_tail_lost,"actions":actions,"firstErrors":errs,"consoleTail":list(tail)}
    write("verdict",receipt)
    print(json.dumps(receipt))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
