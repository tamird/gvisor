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


def bootstrap():
    asset = "bazel-8.8.1-linux-" + ("x86_64" if arch == "amd64" else "arm64")
    manifest = Path("images/default/bazel.sha256")
    original = manifest.read_bytes()
    (sha,) = [line.split()[0] for line in original.decode().splitlines() if line.split()[1] == asset]
    badtmp = out / "bad-install"
    badtmp.mkdir()
    try:
        manifest.write_bytes(original.replace(sha.encode(), b"0"*64))
        require(subprocess.check_output(["git","diff","--name-only"]).splitlines() == [b"images/default/bazel.sha256"], "tamper scope")
        result = run("tampered_pre_hook", ["bash", str(hook), "pre"], env={**os.environ, "TMPDIR": str(badtmp)})
        facts["tamper"] = {"failed_digest": (asset+": FAILED").encode() in result.stdout,
                           "remaining_paths": sum(1 for _ in badtmp.iterdir()), "published_env": (out/"environment").exists()}
        require(result.returncode != 0 and facts["tamper"] == {"failed_digest":True,"remaining_paths":0,"published_env":False}, "tampered hook")
    finally:
        manifest.write_bytes(original)
    run("pre_hook", ["bash", str(hook), "pre"]).check_returncode()
    jobdir, selected, path = (out/"environment").read_text().splitlines()
    binary = Path(selected)
    require(binary == Path(jobdir)/"bazel" and binary.is_file(), "hook selection")
    os.environ.update(PATH=path, BUILDKITE_BAZEL_DIRECTORY=jobdir)
    raw = binary.read_bytes()
    read_sha = hashlib.sha256(raw).hexdigest()
    require(raw[:4]==b"\x7fELF" and raw[4:6] == b"\x02\x01", "ELF")
    machine = struct.unpack_from("<H", raw, 18)[0]
    version = run("installed_version", ["bazel","--version"])
    version.check_returncode()
    require(read_sha == sha and machine == {"amd64":62,"arm64":183}[arch]
            and version.stdout.strip()==b"bazel 8.8.1" and shutil.which("bazel")==selected,"official executable")
    facts["installer"] = {"sha256":read_sha,"bytes":len(raw),"elf_machine":machine,"version":"8.8.1",
                          "mode":oct(binary.stat().st_mode & 0o777)}
    require(facts["installer"]["mode"] == "0o755", "installed mode")
    donor.record()
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
    require(inventories==["inventory"]*3 and not (out/"docker-calls").exists() and not (out/"unselected-bazel").exists(),"unexpected Docker or Bazel")
    trace.unlink(missing_ok=True)


def main():
    errors=[]
    donor.record()
    for do in (source_gate,bootstrap):
        do()
    status=0
    if arch=="amd64":
        for do in (positive,negative):
            try:
                if value:=do():
                    status=value
            except Exception as err:
                errors.append([do.__name__,type(err).__name__,str(err)[:140]])
                donor.record()
    else:
        facts["not_selected"]=["full Make lint and its failure cleanup are required on AMD64"]
    post(status)
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
