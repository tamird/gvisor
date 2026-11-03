"""Run Buildkite's complete Nogo command and report configured vs executed tests."""

import collections
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
import zipfile
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
    assert result[0] == result[1] == ["make", "nogo-tests"]
    return [*result[0], f"STARTUP_OPTIONS=--bazelrc={root}/nogo-resources.bazelrc"]


def valid_log_line(line):
    return not any(k in line.lower() for k in ("http", "token", "api-key", "header=", "bearer", "grpcs:"))


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == os.environ["PRODUCT"]
    cmd = commands()
    owner = None
    previous = root / "original-wildcard.zip"
    assert hashlib.sha256(previous.read_bytes()).hexdigest() == "33eedc60a54fd155ccc34b0e53ca984855e87d6c95614f0c7d4f1bf298859b7f"
    with zipfile.ZipFile(previous) as z:
        old = json.loads(z.read("owners.json"))
        v = json.loads(z.read("verdict.json"))
    assert v["source"] == "8e13db5ad62802add68356df383e4a581e8eb303"
    assert v["invocation"] == "1c14ce87-518f-4e52-bedf-45e54d2390b5"
    expected = set(old["configured"])
    assert len(expected) == 871
    started = int(time.time())
    free_start = shutil.disk_usage(".").free
    with console.open("w") as f:
        status = subprocess.run(["timeout", "--kill-after=30s", os.environ.get("NOGO_TIMEOUT", "110m"), *cmd], stdout=f, stderr=f).returncode
    finished = int(time.time())
    execution_finished = datetime.now(timezone.utc).isoformat()
    write("execution", {"source": os.environ["PRODUCT"], "status": status, "started": started,
                        "finished": finished, "execution_finished": execution_finished})
    configured = set()
    complete = {}
    summaries = {}
    results = collections.defaultdict(list)
    actions = []
    bazel_exit = None
    invocation = None
    runners = []
    event_tail_lost = False
    options = []
    attempts = collections.defaultdict(list)
    if events.is_file():
        with events.open() as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    event_tail_lost = True
                    continue
                ident = ev["id"]
                if "optionsParsed" in ev:
                    options.extend(ev["optionsParsed"].get("explicitCmdLine", []))
                if "targetConfigured" in ident and not ident["targetConfigured"].get("aspect"):
                    configured.add(ident["targetConfigured"]["label"])
                if "targetCompleted" in ident and not ident["targetCompleted"].get("aspect"):
                    complete[ident["targetCompleted"]["label"]] = ev.get("completed", {}).get("success", False)
                if "testSummary" in ev:
                    summaries[ident["testSummary"]["label"]] = ev["testSummary"]["overallStatus"]
                if "testResult" in ev:
                    t = ev["testResult"]
                    attempts[ident["testResult"]["label"]].append(ident["testResult"])
                    results[ident["testResult"]["label"]].append({"status": t.get("status"), "cachedLocally": t.get("cachedLocally", False), "cachedRemotely": t.get("executionInfo", {}).get("cachedRemotely", False), "strategy": t.get("executionInfo", {}).get("strategy")})
                if "action" in ev and not ev["action"].get("success", False) and len(actions) < 50:
                    a = ev["action"]
                    detail = a.get("failureDetail", {})
                    actions.append({"label":ident.get("actionCompleted",{}).get("label"), "mnemonic":a.get("type"), "exit":a.get("exitCode"),
                                    "failureClasses":{k: v.get("code") for k,v in detail.items() if isinstance(v,dict)},
                                    "noUsableStrategy":"cannot be executed with any of the available strategies" in detail.get("message",""),
                                    "stderrAvailable":bool(a.get("stderr",{}))})
                if "started" in ev:
                    invocation = ev["started"].get("uuid")
                if "buildMetrics" in ev:
                    runners = ev["buildMetrics"].get("actionSummary", {}).get("runnerCount", [])
                if "finished" in ev:
                    bazel_exit = ev["finished"]["exitCode"]
    markers = {}
    log_identities = {}
    failures = {}
    for label in sorted(results):
        if not label.startswith("//"):
            continue
        p = Path("bazel-testlogs") / label[2:].replace(":", "/") / "test.log"
        if p.is_file():
            raw = p.read_bytes()
            s = raw.decode(errors="replace")
            log_identities[label] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            markers[label] = {a: a+"..." in s for a in ("amd64", "arm64")}
            if summaries.get(label) != "PASSED":
                failures[label] = [l[:400] for l in s.splitlines() if valid_log_line(l)][:18]
    errs = []
    diagnostics = []
    compiler = re.compile(r"(?:\.go:\d+|compilepkg:|^[ \t]*(?:have|want) \(|missing strict dependenc|not declared as a dependenc|in file .+ import|no such package|could not import|no matching files found|cannot find package|cannot use|undefined:|fatal error:|#error )")
    tail = collections.deque(maxlen=6)
    with console.open(errors="replace") as f:
        for line in f:
            if valid_log_line(line):
                if compiler.search(line) and len(diagnostics) < 30:
                    entry = line.strip()[:500]
                    if entry not in diagnostics:
                        diagnostics.append(entry)
                if ("ERROR:" in line or "not found" in line or "No such file" in line or "No space" in line) and len(errs)<45:
                    errs.append(line.rstrip()[:650])
                if line.strip():
                    tail.append(line.rstrip()[:350])
    sets_agree = configured == set(results) == set(summaries)
    log_arms = all(m.get(a) for m in markers.values() for a in ("amd64","arm64"))
    known = {"//pkg/sentry/loader/vdsodata:vdsodata_nogo","//pkg/sentry/platform/systrap/sysmsg:sysmsg_nogo","//pkg/bits:bits_nogo","//pkg/tcpip/seqnum:seqnum_nogo"}
    if owner:
        known = {owner}
    coverage = (configured == expected if expected is not None else
                configured == {owner} if owner else len(configured) == 871 and known <= configured)
    agent_options = all(option in options for option in ("--remote_cache=grpcs://remote.buildbuddy.io", "--define=nogo_agent=quoted value"))
    ok = status == 0 and bazel_exit and bazel_exit["name"] == "SUCCESS" and sets_agree and coverage and agent_options and set(markers)==configured and log_arms and all(v=="PASSED" for v in summaries.values())
    if os.environ.get("NOGO_FRESH_RESULTS"):
        target_strategy = os.environ.get("NOGO_TEST_STRATEGY", "processwrapper-sandbox")
        ok = bool(ok and all(complete.get(l) is True and len(results[l]) == 1 and
                             results[l][0] == {"status":"PASSED","cachedLocally":False,"cachedRemotely":False,"strategy":target_strategy}
                             for l in configured))
    write("owners", {"configured": sorted(configured), "completed": complete, "summaries":summaries, "results":results, "attempts":attempts, "markers":markers, "logIdentities":log_identities, "failedLogs":failures})
    receipt = {"verified":bool(ok),"source":os.environ["PRODUCT"],"host":platform.machine(),"aarch64_gcc":shutil.which("aarch64-linux-gnu-gcc"),"bazel_command":cmd[2:],"bazel_exit":bazel_exit,"status":status,"started":started,"finished":finished,"filed":int(time.time()),"diskFreeBefore":free_start,"diskFreeAfter":shutil.disk_usage(".").free,"configured":len(configured),"summaries":dict(collections.Counter(summaries.values())),"testRunners":len(results),"amd64Markers":sum(m["amd64"] for m in markers.values()),"arm64Markers":sum(m["arm64"] for m in markers.values()),"eventTailLost":event_tail_lost,"actions":actions,"firstErrors":errs,"consoleTail":list(tail)}
    receipt["invocation"] = invocation
    receipt.update(execution_finished=execution_finished, verdict_available=datetime.now(timezone.utc).isoformat(),
                   agent_options=agent_options, expected=sorted(expected), missing=sorted(expected-set(results)),
                   unexpected=sorted(set(results)-expected),
                   skipped=sorted(label for label, value in summaries.items() if value == "SKIPPED"))
    receipt["processes"] = runners
    receipt["compilerDiagnostics"] = diagnostics
    observed_failure = bool(owner and status == 1 and configured == {owner} and not results and complete.get(owner) is False and diagnostics and actions and all(a["label"] == owner.removesuffix("_nogo") and a["mnemonic"] == "GoCompilePkg" and a["exit"] == 1 for a in actions))
    receipt["outcome"] = "passed" if ok else "compile-failure" if observed_failure else "incomplete"
    write("verdict",receipt)
    print(json.dumps(receipt))
    raise SystemExit(0 if ok or observed_failure else 1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        write("collection-error", {"verified": False, "error_type": type(exc).__name__,
                                   "verdict_available": datetime.now(timezone.utc).isoformat()})
        raise SystemExit(1)
