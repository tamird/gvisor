"""Check the repository's Buildkite hooks in a job-owned Linux environment."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import time

import lint_donor as donor
import qualification_report as reporter

root = Path(os.environ["RUNNER_TEMP"])
out = root / "codespell"
source = os.environ["SOURCE"]
arch = os.environ["ARCH"]
repo = Path.cwd()
hook = Path(os.environ["HOOK"])
facts = donor.facts
facts.update(arch=arch, carrier=os.environ["GITHUB_SHA"], start=time.time())
checks = ["goimports", "clang-format", "cpplint", "buildifier", "spelling"]
os.environ.update(PROBE_OUT=str(out), DOCKER_BUILD="false", BUILDKITE_BRANCH="qualification",
                  BUILDKITE_AGENT_META_DATA_QUEUE="qualification", BUILDKITE_LABEL="qualification",
                  BUILDKITE_JOB_ID="qualification", BUILDKITE_BUILD_ID="qualification",
                  BUILDKITE_PIPELINE_INSTALL_RUNTIME="false", BUILDKITE_COMMAND="make lint")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def run(name, argv, *, env=None):
    began = time.monotonic()
    result = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    facts["phases"][name] = {"exit": result.returncode, "elapsed_s": round(time.monotonic()-began, 3),
                             "output_sha256": hashlib.sha256(result.stdout).hexdigest()}
    donor.record()
    return result


def guard(name, body):
    path = root / "bin" / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def source_gate():
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    require(sha == source, "source")
    expected_machine = {"amd64": "x86_64", "arm64": "aarch64"}[arch]
    require(platform.machine() == expected_machine, "host architecture")
    facts["host"] = platform.machine()
    files = [".buildkite/pipeline.yaml", ".buildkite/first-pass.yaml"]
    facts["steps"] = {}
    for file in files:
        stanzas = re.split(r"(?m)^  - ", Path(file).read_text())
        (step,) = [s for s in stanzas if re.search(r"(?m)^    command: make lint$", s)]
        require('    env:\n      DOCKER_BUILD: "false"\n' in step, "step environment")
        facts["steps"][file] = "make lint, DOCKER_BUILD=false"
    guard("docker", 'echo invoked >> "$PROBE_OUT/docker-calls"; exit 87\n')
    guard("bazel", 'echo invoked >> "$PROBE_OUT/unselected-bazel"; exit 87\n')
    os.environ["PATH"] = str(root/"bin") + os.pathsep + os.environ["PATH"]


def pre(name, env):
    (out/"environment").unlink(missing_ok=True)
    run(name, ["bash", str(hook), "pre"], env=env).check_returncode()
    job, selected, path, override, fallback = (out/"environment").read_text().splitlines()
    require(override == "" and fallback == "error:", "checkout pin policy")
    binary = Path(selected)
    require(binary == Path(job)/"bazel" and binary.is_file(), "hook selection")
    os.environ.pop("USE_BAZEL_VERSION", None)
    os.environ.update(PATH=path, BUILDKITE_BAZEL_DIRECTORY=job, USE_BAZEL_FALLBACK_VERSION=fallback)
    return binary


def bootstrap():
    normal_path = os.environ["PATH"]
    go = shutil.which("go")
    require(go is not None, "agent Go")
    tools = root/"fallback-tools"
    tools.mkdir()
    (tools/"go").symlink_to(go)
    fallback_path = os.pathsep.join((str(root/"bin"), str(tools), "/usr/bin", "/bin"))
    require(shutil.which("bazelisk", path=fallback_path) is None, "fallback must start without Bazelisk")
    env = {**os.environ, "PATH":fallback_path, "USE_BAZEL_VERSION":"disallowed-agent-override"}
    badtmp = out/"failed-install"
    badtmp.mkdir()
    # Reject the real Go dependency resolution before it can install anything.
    bad = run("unavailable_bootstrap", ["bash", str(hook), "pre"],
              env={**env, "TMPDIR":str(badtmp), "GOMODCACHE":str(out/"empty-modules"), "GOPROXY":"off"})
    require(bad.returncode != 0 and (b"GOPROXY=off" in bad.stdout or b"module lookup disabled" in bad.stdout)
            and not list(badtmp.iterdir()) and not (out/"environment").exists(), "failed bootstrap cleanup")

    binary = pre("fallback_pre", env)
    raw = binary.read_bytes()
    machine = struct.unpack_from("<H", raw, 18)[0]
    require(not binary.is_symlink() and raw[:6] == bytes.fromhex("7f454c460201")
            and machine == {"amd64":62,"arm64":183}[arch], "fallback ELF")
    desc = subprocess.check_output([go, "version", "-m", binary],text=True)
    require("\tpath\tgithub.com/bazelbuild/bazelisk" in desc and
            "\tmod\tgithub.com/bazelbuild/bazelisk\tv1.30.0\th1:" in desc, "installed official module and sum")
    chosen = Path(".bazelversion").read_text().strip()
    require(chosen=="8.8.1", "source pin")
    ver = run("fallback_version", ["bazel","--version"])
    require(ver.returncode == 0 and ver.stdout.splitlines()[-1] == f"bazel {chosen}".encode(), "official Bazel resolution")
    # Remove only the symlink in the disposable checkout, leaving the actual
    # pin untouched. A missing version must not run a different release.
    pin = Path(".bazelversion")
    dest = os.readlink(pin)
    pin.unlink()
    try:
        none = run("missing_checkout_pin", ["bazel","--version"])
        require(none.returncode != 0 and b"not allowed to use fallback version" in none.stdout, "missing pin failure")
    finally:
        pin.symlink_to(dest)
    provision = root/"available"
    provision.mkdir()
    launcher = provision/"bazelisk"
    shutil.copy2(binary, launcher)
    facts["fallback"] = {"module":next(line.strip() for line in desc.splitlines() if line.startswith("\tmod\t")),
                         "elf_machine":machine,"sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw),"selected_bazel":chosen,
                         "missing_pin_exit":none.returncode,"failed_install_exit":bad.returncode}
    donor.record()
    run("fallback_post", ["bash",str(hook),"post"], env={**os.environ,"BUILDKITE_COMMAND_EXIT_STATUS":"0"}).check_returncode()
    require(not binary.parent.exists() and launcher.is_file(),"fallback cleanup")
    (provision/"go").write_text("#!/bin/sh\necho invoked >> \"$PROBE_OUT/unexpected-go-install\"; exit 85\n")
    (provision/"go").chmod(0o755)
    env = {**os.environ,"PATH":str(provision)+os.pathsep+normal_path,"USE_BAZEL_VERSION":"disallowed-agent-override"}
    selected = pre("provisioned_pre", env)
    require(selected.is_symlink() and selected.resolve()==launcher and not (out/"unexpected-go-install").exists(),"reuse launcher")
    # The proof guards installation only in pre-command. Restore the real Go
    # to ordinary subprocesses without changing the selected Bazel on PATH.
    (provision/"go").unlink()
    facts["provisioned"] = {"sha256":hashlib.sha256(launcher.read_bytes()).hexdigest(),"external_launcher":str(launcher)}
    info=run("release_info",["bazel","info","release"])
    require(info.returncode==0 and info.stdout.splitlines()[-1] == b"release 8.8.1","native release info")


def positive():
    # Import the exact previous qualification of the default Make target,
    # tracked misspelling / ignore list, no auto-fix, and Docker exclusion.
    t=time.monotonic()
    donor.lint()
    facts["phases"]["lint_driver"]={"elapsed_s":round(time.monotonic()-t,3),"exit":0}
    events = out / "events.jsonl"
    base=Path(subprocess.check_output(["bazel","info","output_base"],text=True).strip())
    captured=reporter.capture(events,out/"clean.jsonl",base,out/"reports")
    statuses={}
    for line in (out/"clean.jsonl").read_text().splitlines():
        e=json.loads(line)
        if entry:=e.get("started"):
            facts["positive_invocation"]=entry["uuid"]
        if test:=e.get("testResult"):
            statuses[e["id"]["testResult"]["label"]]=test["status"]
    reports={}
    for item in captured["reports"]:
        if item["name"] in ("test.log","test.xml"):
            reports.setdefault(item["identity"]["label"],{})[item["name"]]={k:item[k] for k in ("bytes","sha256")}
    require(statuses and all(v=="PASSED" and set(reports.get(k,{}))=={"test.log","test.xml"} for k,v in statuses.items()),
            "positive BEP")
    facts["last_lint_tests"]={"statuses":statuses,"reports":reports}
    donor.record()


def negative():
    readme=Path("README.md")
    original=readme.read_bytes()
    try:
        readme.write_bytes(original+b"\nThis is teh spelling probe.\n")
        p=run("negative_make",["make","lint"])
        passes=[v.decode() for v in re.findall(rb"PASS  (\S+)",p.stdout)]
        failed=[v.decode() for v in re.findall(rb"FAIL  (\S+)",p.stdout)]
        facts["failing_lint"]={"pass":passes,"fail":failed,"diagnostic":b"README.md:" in p.stdout and b"teh ==> the" in p.stdout}
        require(p.returncode != 0 and passes==checks[:-1] and failed==["spelling"] and facts["failing_lint"]["diagnostic"],"negative Make")
        return p.returncode
    finally:
        readme.write_bytes(original)


def post(status):
    jobdir=Path(os.environ["BUILDKITE_BAZEL_DIRECTORY"])
    trace=out/"exec.trace"
    argv=["bash",str(hook),"post"]
    if status:
        argv=["strace","-f","-e","trace=execve","-s","240","-o",str(trace),*argv]
    p=run("post_hook",argv,env={**os.environ,"BUILDKITE_COMMAND_EXIT_STATUS":str(status)})
    facts["cleanup"]={"directory_removed":not jobdir.exists(),"hook_exit":p.returncode}
    require(not jobdir.exists() and p.returncode==0,"post hook")
    if status:
        lines=trace.read_text().splitlines()
        for subcmd in ("clean","shutdown"):
            rx=rf'^\s*(\d+)\s+execve\("{re.escape(str(jobdir/"bazel"))}", \["bazel", "{subcmd}"\]'
            candidates=[m.group(1) for line in lines if (m:=re.match(rx,line))]
            require(len(candidates)==1, "selected exec "+subcmd)
            pid=candidates[0]
            (exitcode,)=[int(m.group(1)) for line in lines if (m:=re.match(rf'^\s*{pid}\s+\+\+\+ exited with (\d+) \+\+\+',line))]
            facts["cleanup"][subcmd]={"executed_installed_bazel":True,"exit":exitcode}
            require(exitcode==0, "native "+subcmd+" exit")
        require(not b"make clean failed with code" in p.stdout,"make clean")
    inventories=(out/"hook-docker").read_text().splitlines()
    facts["housekeeping_docker_inventory_calls"]=len(inventories)
    require(inventories==["inventory"]*5 and not (out/"docker-calls").exists() and not (out/"unselected-bazel").exists(),"unexpected Docker or Bazel")
    trace.unlink(missing_ok=True)


def main():
    errors=[]
    donor.record()
    for do in (source_gate,bootstrap):
        do()
    status=0
    for do in (positive,negative):
        try:
            if value:=do():
                status=value
        except Exception as err:
            errors.append([do.__name__,type(err).__name__,str(err)[:140]])
            donor.record()
    post(status)
    preexisting = Path(facts["provisioned"]["external_launcher"])
    require(hashlib.sha256(preexisting.read_bytes()).hexdigest()==facts["fallback"]["sha256"], "existing launcher unchanged")
    require(not subprocess.check_output(["git","diff","--name-only"]), "source was not restored")
    facts["errors"]=errors
    require(not errors,"lint checks")
    facts["status"]="PASS"


if __name__=="__main__":
    try:
        main()
    except Exception as e:
        facts["status"]="FAIL"
        facts["failure"]=[type(e).__name__,e.__traceback__.tb_lineno,str(e)[:140]]
        raise
    finally:
        facts["finish"]=time.time()
        donor.record()
