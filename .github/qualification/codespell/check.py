"""Exercise the declared wheel and source-only Make entrypoint on Linux."""
import email
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request
import zipfile

import qualification_report as reporter

out = Path(os.environ["RUNNER_TEMP"]) / "codespell"
source = os.environ["SOURCE"]
wheel = "codespell-2.3.0-py3-none-any.whl"
wheel_sha = "a9c7cef2501c9cfede2110fd6d4e5e62296920efe9abfb84648df866e47f58d1"
facts = {"source": source, "status": "INCOMPLETE", "phases": {}}


def record():
    (out / "result.json").write_text(json.dumps(facts, sort_keys=True, indent=2) + "\n")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def phase(name, *args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    facts["phases"][name] = {"exit": result.returncode, "output_sha256": digest(result.stdout)}
    record()
    if result.returncode:
        lines = result.stdout.decode(errors="replace").splitlines()
        markers = ("missing entry for", "requires an exception", "unknown license", "no such package", "unrecognized option", "FAIL  ", "checksum mismatch", "not found in")
        facts["phases"][name]["diagnostics"] = [marker for marker in markers if any(marker in line for line in lines)]
        facts["phases"][name]["codespell_policy_errors"] = [line[:200] for line in lines if line.startswith(("pypi/codespell", "missing entry for pypi/codespell", "stale entry for pypi/codespell"))]
        record()
    return result


def check(name, *args):
    result = phase(name, *args)
    result.check_returncode()
    return result.stdout


def native_units():
    labels = ("//tools:lint_test", "//tools/licensecheck:licensecheck_test")
    native = phase("units", "bazel", "test", "--test_output=errors", *labels)
    events = out / "events.jsonl"
    base = Path(subprocess.check_output(["bazel", "info", "output_base"]).decode().strip())
    capture = reporter.capture(events, out / "clean.jsonl", base, out / "reports")
    refs = {}
    for item in capture["reports"]:
        if item["name"] in ("test.log", "test.xml"):
            refs.setdefault(item["identity"]["label"], {})[item["name"]] = {k:item[k] for k in ("sha256","bytes")}
    statuses = {}
    for evt in map(json.loads, (out/"clean.jsonl").read_text().splitlines()):
        if started := evt.get("started"):
            facts["unit_invocation"] = started["uuid"]
        if tr := evt.get("testResult"):
            statuses[evt["id"]["testResult"]["label"]] = tr["status"]
    facts["unit_reports"] = refs
    facts["unit_status"] = statuses
    record()
    assert native.returncode == 0 and statuses == dict.fromkeys(labels,"PASSED")
    assert all(set(refs[label]) == {"test.log","test.xml"} for label in labels)


def wheel_license():
    catalog = Path("tools/licensecheck/dependencies.yaml").read_text()
    url = catalog.split("- dependency: pypi/" + wheel + "\n",1)[1].split("version: ",1)[1].splitlines()[0]
    assert url.startswith("https://files.pythonhosted.org/") and url.endswith("/"+wheel)
    with urllib.request.urlopen(url,timeout=45) as src:
        raw = src.read()
    assert digest(raw)==wheel_sha
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        md, = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
        metadata = email.message_from_bytes(z.read(md))
        assert metadata["Name"] == "codespell" and metadata["Version"]=="2.3.0"
        names = metadata.get_all("License-File")
        assert names
        notices = {}
        for name in names:
            candidates = [f for f in (md.rsplit("/",1)[0]+"/"+name, md.rsplit("/",1)[0]+"/licenses/"+name) if f in z.namelist()]
            assert len(candidates)==1
            data=z.read(candidates[0])
            notices[name] = {"sha256":digest(data),"gpl2":b"GNU GENERAL PUBLIC LICENSE" in data and b"Version 2, June 1991" in data}
        facts["wheel"] = {"sha256":wheel_sha,"size":len(raw),"license":metadata["License"],"notices":notices,"dictionary_sha256":digest(z.read("codespell_lib/data/dictionary.txt"))}
        record()
        assert metadata["License"]=="GPL-2.0-only" and any(n["gpl2"] for n in notices.values())
    # Verify the real distribution is present in the hub, not just documented.
    run = check("version","bazel","run","--run_in_cwd","//tools/lint:codespell","--","--version")
    assert any(re.fullmatch(rb"(?:codespell\s+)?2[.]3[.]0", line.strip(), re.I) for line in run.splitlines())


def interpreter():
    launch=out/"codespell.sh"
    check("launcher","bazel","run","--run_in_cwd","--script_path="+str(launch),"//tools/lint:codespell")
    guard=out/"path"; guard.mkdir()
    for name in ("python","python3"):
        f=guard/name; f.write_text("#!/bin/sh\nexit 71\n");f.chmod(0o755)
    env={**os.environ,"PATH":str(guard)+os.pathsep+os.environ["PATH"]}
    p=subprocess.Popen([launch,"--config","tools/.codespellrc","-"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
    runtime=None
    try:
        for _ in range(200):
            queue=[p.pid]
            while queue:
                pid=queue.pop()
                try:
                    exe=Path(os.readlink(f"/proc/{pid}/exe"))
                    if exe.name.startswith("python3") and "/external/" in str(exe):
                        runtime=exe; break
                    children=Path(f"/proc/{pid}/task/{pid}/children").read_text()
                    queue.extend(map(int,children.split()))
                except (FileNotFoundError,ProcessLookupError):
                    pass
            if runtime is not None or p.poll() is not None:break
            time.sleep(.025)
        stdout,stderr=p.communicate(b"Spelling is correct.\n",timeout=15)
    finally:
        if p.poll() is None:p.kill();p.wait()
    facts["interpreter"]={"exit":p.returncode,"declared_external":runtime is not None,"basename":runtime.name if runtime else None}
    if runtime:
        ver=subprocess.check_output([runtime,"--version"]).decode().strip()
        facts["interpreter"].update(version=ver,sha256=digest(runtime.read_bytes()))
    record()
    assert p.returncode==0 and runtime and ver.startswith("Python 3.11.")
    # Exercise the repository's existing case-sensitive ignore list using a
    # real default-dictionary entry, not a synthetic configuration.
    hits=[]
    for word in ("parth", "FillIn", "offsetP", "canBe", "addin", "refErr"):
        plain=subprocess.run([launch,"-"],input=(word+"\n").encode(),capture_output=True,env=env)
        configured=subprocess.run([launch,"--config","tools/.codespellrc","-"],input=(word+"\n").encode(),capture_output=True,env=env)
        if plain.returncode and configured.returncode==0 and b"==>" in plain.stdout+plain.stderr:
            hits.append(word)
    facts["allowlist_observed"] = hits
    record()
    assert hits


def lint():
    docker = Path(os.environ["RUNNER_TEMP"])/"bin/docker"
    calls=out/"docker-calls"
    docker.write_text("#!/bin/sh\necho invoked >> '"+str(calls)+"'\nexit 87\n")
    docker.chmod(0o755)
    inputs=subprocess.check_output(["git","ls-files","-z","--","*.md","*.html","*.go","*.c","*.cc","*.h"]).split(b"\0")[:-1]
    facts["inputs"]={"count":len(inputs),"sha256":digest(b"\0".join(inputs))}
    run=phase("default_lint","make","lint")
    passes=re.findall(rb"PASS  (\S+)",run.stdout)
    facts["default_passes"]=[x.decode() for x in passes]
    assert run.returncode == 0 and passes==[b"goimports",b"clang-format",b"cpplint",b"buildifier",b"spelling"]
    path=Path("README.md"); original=path.read_bytes()
    extra=Path("codespell-untracked-probe.md"); assert not extra.exists()
    bad=b"\nThis is teh spelling probe.\n"
    try:
        extra.write_bytes(bad);path.write_bytes(original+bad+b"\nThe name parth is accepted.\n")
        badrun=phase("tracked_negative","tools/lint.sh","spelling")
        facts["negative"]={"tracked":b"README.md:" in badrun.stdout and b"teh ==> the" in badrun.stdout,"untracked_reported":extra.name.encode() in badrun.stdout,"ignored_name_reported":b"parth ==>" in badrun.stdout,"unchanged":path.read_bytes()==original+bad+b"\nThe name parth is accepted.\n"}
        fix=phase("fix_spelling","tools/lint.sh","--fix","spelling")
        facts["explicit_fix_refused"]=fix.returncode==1 and b"--fix applies only to" in fix.stdout and path.read_bytes()==original+bad+b"\nThe name parth is accepted.\n"
        record()
        assert badrun.returncode==1 and facts["negative"]=={"tracked":True,"untracked_reported":False,"ignored_name_reported":False,"unchanged":True} and facts["explicit_fix_refused"]
        path.write_bytes(original)
        check("untracked_is_ignored","tools/lint.sh","spelling")
    finally:
        path.write_bytes(original); extra.unlink(missing_ok=True)
    facts["docker_invocations"]=len(calls.read_text().splitlines()) if calls.exists() else 0
    record()
    assert not calls.exists()


def main():
    assert subprocess.check_output(["git","rev-parse","HEAD"]).decode().strip()==source
    record()
    errors=[]
    for fn in (wheel_license,native_units,interpreter,lint):
        try:
            fn()
        except Exception as exc:
            errors.append([fn.__name__,type(exc).__name__,exc.__traceback__.tb_next.tb_lineno if exc.__traceback__.tb_next else 0])
            facts["errors"]=errors; record()
    catalog=phase("catalog","make","DOCKER_BUILD=false","license-check")
    m=re.search(rb"all (\d+) dependencies have up-to-date license entries",catalog.stdout)
    facts["catalog_count"]=int(m[1]) if m else None
    if errors or catalog.returncode or not m:
        facts["status"]="FAIL"; record(); raise RuntimeError("see typed qualification result")
    facts["status"]="PASS"; record()


if __name__=="__main__":
    try:
        main()
    finally:
        record()
