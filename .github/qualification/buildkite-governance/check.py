"""Exercise both native governance commands and the failed-log handoff."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import time

import lint_hooks as hooks

facts = hooks.facts
require = hooks.require
out = hooks.out
names = ("CODEOWNERS", "MAINTAINERS.md")
owners = {f"//:governance-regen_{i}_test": name for i, name in enumerate(names)}
marker = b"# qualification: stale CODEOWNERS"
steps = (".buildkite/first-pass.yaml", ".buildkite/pipeline.yaml")


def git(*args):
    return subprocess.check_output(["git", *args]).decode().strip()


def command(file):
    stanzas = re.split(r"(?m)^  - ", Path(file).read_text())
    (step,) = [s for s in stanzas if 'label: ":scroll: Governance file sync check"' in s]
    (cmd,) = re.findall(r"(?m)^    command: (.+)$", step)
    require(shlex.split(cmd) == ["make", "governance-check"], "shipped command")
    require('<<: *source_test\n' in step and '    env:\n      DOCKER_BUILD: "false"\n' in step, "source gating")
    require('  queue: "untrusted"\n' in Path(file).read_text() if file==steps[0] else '      arch: "amd64"' in step, "configured agent")
    return cmd


def state():
    return git("status", "--porcelain", "--untracked-files=no")


def comparison(phase, cmd, want_failure):
    bep = Path(".build_events.json")
    bep.unlink(missing_ok=True)
    result = hooks.run(phase, shlex.split(cmd))
    base = Path(subprocess.check_output(["bazel", "info", "output_base"], text=True).strip())
    os.environ["PROBE_BASE"]=str(base)
    # Make is the only source of these runner-provided options. Inspect the
    # command Bazel parsed without retaining any raw credential or argv.
    parsed = [json.loads(line)["optionsParsed"] for line in bep.read_text().splitlines() if '"optionsParsed":' in line and "optionsParsed" in json.loads(line)]
    (original,) = parsed
    opts = original["explicitCmdLine"]
    routed = "--remote_cache=grpcs://remote.buildbuddy.io" in opts and "--define=governance_probe=agent value" in opts
    clean = out/(phase + ".jsonl")
    captured = hooks.reporter.capture(bep, clean, base, out/(phase+"-reports"))
    row = facts.setdefault("comparisons", {})[phase] = {"exit":result.returncode,"owners":{},"capture_errors":len(captured["errors"])}
    row["agent_options_received"]=routed
    hooks.donor.record()
    require(routed, "lost the agent cache setting or quoted option")
    require(not captured["errors"], "could not capture all original reports")
    for line in clean.read_text().splitlines():
        e = json.loads(line)
        if "started" in e:
            row["uuid"] = e["started"]["uuid"]
        if finish := e.get("finished"):
            row["finished"] = finish.get("finishTime", finish.get("finishTimeMillis"))
            row["bazel_exit"] = finish["exitCode"]["code"] if "code" in finish["exitCode"] else 0
        if r := e.get("testResult"):
            label = e["id"]["testResult"]["label"]
            require(label in owners and label not in row["owners"], "unexpected test owner or duplicate attempt")
            row["owners"][label] = {"file":owners[label],"status":r["status"],"runner":r.get("executionInfo",{}).get("strategy"),
                                    "cached":bool(r.get("cachedLocally") or r.get("executionInfo",{}).get("cachedRemotely"))}
    failed_path = None
    for report in captured["reports"]:
        label = report["identity"]["label"]
        r = row["owners"][label]
        r[report["name"]] = {"bytes":report["bytes"], "sha256":report["sha256"]}
        if r["status"]=="FAILED" and report["name"]=="test.log":
            log = (out/report["path"]).read_bytes()
            require(marker in log and b"CODEOWNERS" in log, "negative diff missing marker")
            # Compare the hook's native log, the captured bytes and the source failure.
            failed_path = subprocess.check_output(["make","-s","testlogs"],text=True).splitlines()
            require(len(failed_path)==1, "expected one failed log")
            got, path = failed_path[0].split()
            require(got==label and Path(path).is_absolute() and Path(path).resolve().is_relative_to(base) and
                    Path(path).read_bytes()==log and Path(path).name=="test.log", "local failure handoff")
            row["failed_log"] = {"label":label,"local_path":str(Path(path).resolve().relative_to(base)),"sha256":report["sha256"]}
    hooks.donor.record()
    expected = {k:("FAILED" if want_failure and v==names[0] else "PASSED") for k,v in owners.items()}
    require({k:r["status"] for k,r in row["owners"].items()}==expected and
            result.returncode==(2 if want_failure else 0) and row["bazel_exit"]==(3 if want_failure else 0) and bool(row.get("uuid")) and bool(row.get("finished")), "original test results")
    require(all(r["runner"]=="remote" and not r["cached"] and "test.xml" in r and "test.log" in r for r in row["owners"].values()), "two fresh remote comparisons")
    require(bool(failed_path)==want_failure, "unexpected log count")
    if not want_failure:
        require(not subprocess.check_output(["make","-s","testlogs"]).strip(), "clean testlogs")
    return result.returncode


def job(file, index):
    cmd = command(file)
    os.environ["BUILDKITE_COMMAND"] = cmd
    os.environ["BUILDKITE_LABEL"] = ":scroll: Governance file sync check"
    launcher = hooks.pre(f"pre_{index}", {**os.environ, "USE_BAZEL_VERSION":"disallowed-agent-override"})
    require(launcher.is_symlink() and launcher.resolve() == Path(shutil.which("bazelisk")).resolve(), "provisioned launcher")
    info=hooks.run(f"actual_bazel_{index}",["bazel","--version"])
    require(info.returncode==0 and info.stdout.splitlines()[-1] == b"bazel "+Path(".bazelversion").read_bytes().strip(), "checkout pin")
    status=0
    try:
        comparison(f"clean_{index}",cmd,False)
        if index==1:
            path=Path(names[0]); clean=path.read_bytes()
            try:
                path.write_bytes(clean+b"\n"+marker+b"\n")
                require(state()=="M CODEOWNERS", "one dirty tracked file")
                status=comparison("stale_CODEOWNERS",cmd,True)
            finally:
                path.write_bytes(clean)
    finally:
        # Capture the PID without creating another server after cleanup.
        before=subprocess.check_output(["bazel","info","server_pid"],text=True).strip()
        require(before.isdecimal(), "Bazel server PID")
        if status:
            hooks.post(status)
        else:
            hooks.run(f"post_{index}",["bash",str(hooks.hook),"post"],env={**os.environ,"BUILDKITE_COMMAND_EXIT_STATUS":"0"}).check_returncode()
        proc=Path("/proc")/before/"stat"
        stopped=not proc.exists() or proc.read_text().split(") ")[-1][0] in "ZX"
        require(stopped and not launcher.parent.exists(), "server or launcher left after cleanup")
        facts.setdefault("jobs",{})[file]={"command":cmd,"host":platform.machine(),"post_status":status,"server_stopped":stopped,"launcher_directory_removed":True}
        os.environ["PATH"]=os.environ["PATH"].removeprefix(str(launcher.parent)+os.pathsep)


def main():
    require(state()=="" and git("rev-parse","HEAD")==hooks.source and platform.machine()=="x86_64","clean source and AMD64 host")
    require(git("rev-parse","HEAD^").startswith("471d6dff5"),"source base")
    facts["files"]={name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in names}
    hooks.guard("docker",'echo invoked >> "$PROBE_OUT/docker-calls"; exit 87\n')
    hooks.guard("bazel",'echo invoked >> "$PROBE_OUT/unselected-bazel"; exit 87\n')
    os.environ["PATH"]=str(hooks.root/"bin")+os.pathsep+os.environ["PATH"]
    require(shutil.which("bazelisk"), "provisioned native Bazelisk")
    make_probe=out/"effective.mk"
    make_probe.write_text("$(info EFFECTIVE_DOCKER_BUILD=$(DOCKER_BUILD))\n")
    for name,opts,want in [("help_environment",[],"false"),("help_command_line",["DOCKER_BUILD=true"],"true")]:
        # Suppress rebuilding the Docker makefile only for the explicit Docker
        # control; ordinary 'env DOCKER_BUILD=true make help' must avoid it.
        argv=["make","-s","-f","Makefile","-f",str(make_probe)]
        if opts:
            argv+=["-o","bazel-server-inc"]
        r=hooks.run(name,[*argv,"help",*opts],env={**os.environ,"DOCKER_BUILD":"true"})
        require(r.returncode==0 and f"EFFECTIVE_DOCKER_BUILD={want}".encode() in r.stdout.splitlines(),"Make precedence")
    token = secrets.token_hex(24)
    os.environ.update(BAZEL_REMOTE_CACHE=f"--remote_cache=grpcs://remote.buildbuddy.io --remote_cache_header=x-governance-test={token}",
                      BAZEL_OPTIONS='--define="governance_probe=agent value"')
    original_run=hooks.run
    def check_output(name,argv,*,env=None):
        result=original_run(name,argv,env=env)
        require(token.encode() not in result.stdout, "agent cache header was printed")
        return result
    hooks.run=check_output
    for index,file in enumerate(steps):
        job(file,index)
    failed=facts["comparisons"]["stale_CODEOWNERS"]["failed_log"]
    uploads=(out/"upload.tsv").read_text().splitlines()
    require(uploads==[failed["sha256"]+"\t"+failed["local_path"]], "post hook did not request exact failed test log")
    facts["post_upload"]=failed
    facts["agent_header_not_printed"]=True
    require(not state(), "stale file not restored")
    facts["status"]="PASS"


if __name__=="__main__":
    try:
        main()
    except Exception as err:
        facts["status"]="FAIL"
        facts["error"]={"type":type(err).__name__,"line":err.__traceback__.tb_lineno,
                        "check":str(err)[:160] if isinstance(err,ValueError) else None}
        raise
    finally:
        facts["finish"]=time.time()
        hooks.donor.record()
