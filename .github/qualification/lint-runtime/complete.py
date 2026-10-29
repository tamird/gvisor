"""Compare the real registered interpreters, native differ, and catalog."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

import check as q

q.OUT["expected"] = ["normal-gnu", "registered-static", "dirty-static", "diff", "catalog"]
q.OUT["manual_runtime_overrides"] = []
q.OUT["errors"] = []

def sanitize(output):
    return [line[:300] for line in output.splitlines() if not any(key in line.lower() for key in
            ("header=", "api-key", "authorization", "credential", "token", "running command line"))][-12:]

def run_tools():
    phase = {"started": q.utc(), "invocation": str(uuid.uuid4())}
    q.OUT["tools"] = phase
    cmd = ["bazel", "--bazelrc="+os.environ["QA_RC"], "build", "--config=x86_64", "--jobs=24",
           "--remote_download_outputs=toplevel", "--remote_default_exec_properties=OSFamily=linux",
           "--remote_default_exec_properties=Arch=amd64", "--remote_default_exec_properties=container-image="+q.GNU_IMAGE,
           "--remote_default_exec_properties=workload-isolation-type=firecracker",
           "--remote_default_exec_properties=EstimatedMemory=2GB", "--invocation_id="+phase["invocation"],
           "@buildifier_prebuilt//buildifier:native", "//tools/licensecheck/main:licensecheck"]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    phase.update(exit=result.returncode, finished=q.utc(), diagnostics=sanitize(result.stdout))
    q.save()
    q.check(result.returncode==0, "tool builds failed")

def catalog():
    phase = dict(start=q.utc())
    q.OUT["phases"]["catalog"] = phase
    cmd = Path("bazel-bin/tools/licensecheck/main/licensecheck_/licensecheck").resolve()
    q.check(cmd.is_file(), "missing compiled canonical verifier")
    env = os.environ.copy()
    # The licensecheck on this source invokes 'make mod'; use the project's
    # documented native Make mode for the nested Bazel module calls.
    env.update(MAKEFLAGS="-- DOCKER_BUILD=false", BUILD_WORKSPACE_DIRECTORY=str(Path.cwd()))
    result = subprocess.run([cmd, "--mode=verify"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, timeout=900)
    matched = re.search(r"all ([0-9]+) dependencies have up-to-date license entries", result.stdout)
    phase.update(exit=result.returncode, dependencies=int(matched[1]) if matched else None, finish=q.utc(),
                 diagnostics=sanitize(result.stdout), verified=bool(matched) and result.returncode==0)
    q.save()
    q.check(phase["verified"], "catalog did not pass")

def diff():
    phase = dict(start=q.utc())
    q.OUT["phases"]["diff"] = phase
    # The path returned by cquery is a source or an output relative to the
    # execution root; resolve symlinks while the Linux checkout is available.
    paths = subprocess.check_output(["bazel", "--bazelrc="+os.environ["QA_RC"], "cquery",
                                     "--config=x86_64", "--output=files", "@buildifier_prebuilt//buildifier:native"], text=True)
    execroot = subprocess.check_output(["bazel", "--bazelrc="+os.environ["QA_RC"], "info", "execution_root"], text=True).strip()
    tool = next(Path(execroot)/s.strip() for s in paths.splitlines() if (Path(execroot)/s.strip()).is_file())
    q.check(sha256(tool.read_bytes()).hexdigest()==q.BUILDIFIER, "wrong supplier executable")
    root = q.ROOT/"buildifier-diff-root"
    root.mkdir()
    (root/"tmp").mkdir()
    path = root/"BUILD"
    path.write_text('filegroup(name="a")\n')
    shutil.copy(tool, root/"buildifier")
    header = ["sudo", "env", "-i", "PATH=/nonexistent", "TMPDIR=/tmp", "/usr/sbin/chroot", str(root), "/buildifier", "--config=off"]
    records = {}
    for mode in ["check", "diff"]:
        proc = subprocess.run([*header, "--mode="+mode, "--", "/BUILD"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        records[mode] = dict(exit=proc.returncode, output=proc.stdout.strip())
    phase.update(records=records, sha256=q.BUILDIFIER, finish=q.utc())
    q.save()
    q.check(records["check"]["exit"]==4 and "/BUILD # reformat" in records["check"]["output"], "missing actionable native diagnostic")
    q.check(records["diff"]["exit"] and "/usr/bin/env" in records["diff"]["output"] and "no such file" in records["diff"]["output"] and
            "@@" not in records["diff"]["output"], "unexpected bare differ")
    phase["verified"]=True
    q.save()

def lint():
    tracked = {p.decode() for p in q.git("ls-files","-z").split(b"\0") if p}
    q.build("normal-gnu", tracked, {})
    q.build("registered-static", tracked, {})
    q.CASE.mkdir()
    (q.CASE/"BUILD.bazel").write_text("# New package.\n")
    Path(q.BAD_CPP).write_text("// New source.\n#include <vector>\n#include <stdio.h>\n")
    try:
        q.build("dirty-static", tracked | {str(q.CASE/"BUILD.bazel"),q.BAD_CPP}, {"Cpplint":1})
    finally:
        Path(q.BAD_CPP).unlink()
        (q.CASE/"BUILD.bazel").unlink()
        q.CASE.rmdir()

def guard(name, func):
    try:
        func()
    except (OSError, ValueError, RuntimeError, KeyError, IndexError, StopIteration, subprocess.SubprocessError) as exc:
        q.OUT["errors"].append(name+": "+str(exc)[:280])
        print(q.OUT["errors"][-1],flush=True)
    q.save()

q.check(q.git("rev-parse","HEAD","HEAD^{tree}").decode().splitlines()==[q.SHA,q.TREE], "wrong product source")
q.save()
guard("lint", lint)
guard("tools", run_tools)
guard("diff", diff)
guard("catalog", catalog)
q.OUT["checks_finished_at"] = q.utc()
q.OUT["verdict"] = "PASS" if not q.OUT["errors"] and all(q.OUT["phases"].get(k,{}).get("verified") for k in q.OUT["expected"]) else "FAIL"
q.save()
raise SystemExit(q.OUT["verdict"] != "PASS")
