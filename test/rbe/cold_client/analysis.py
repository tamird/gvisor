"""Compare Bazel repository queries in fresh Linux roots on the same runner."""

import datetime
import hashlib
import re
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

SOURCE = "f906c5dd87aeffc3a6f5cec8275b6102a278ea0a"
BAZEL = "41ad5f9e833fcaee7398111343570cb155ca14985abb084b1f0ad2d29eaaec6d"
assert os.uname().machine == "aarch64"
tree = Path.cwd()
staging = Path(tempfile.mkdtemp(dir=os.environ["RUNNER_TEMP"], prefix="repo-fetch-"))
evidence = Path(os.environ["RUNNER_TEMP"]) / "client-evidence"
evidence.mkdir()
root = staging / "image"
root.mkdir()
def cmd(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)
def digest(data):
    return hashlib.sha256(data).hexdigest()
def add_file(path, target=None):
    dest = root / str(target or path).lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
client = staging / "bazel"
cmd("curl", "-fsSL", "--retry", "3", "-o", client, "https://releases.bazel.build/8.8.1/release/bazel-8.8.1-linux-arm64")
assert digest(client.read_bytes()) == BAZEL
client.chmod(0o755)
add_file(client, "/probe/bazel")
# The Bazel native launcher and its embedded JDK are explicitly allowed.
# Include shared libraries only; copying whole /usr/lib would also supply git.
for soname in ("ld-linux-aarch64.so.1", "libc.so.6", "libm.so.6", "libz.so.1",
               "libdl.so.2", "libpthread.so.0", "libgcc_s.so.1", "libstdc++.so.6",
               "libresolv.so.2", "librt.so.1", "libutil.so.1", "libnss_files.so.2",
               "libnss_dns.so.2", "libtinfo.so.6", "libcrypt.so.1", "libzstd.so.1"):
    src = Path("/lib/aarch64-linux-gnu") / soname
    if src.exists():
        add_file(src)
add_file("/lib/ld-linux-aarch64.so.1")
add_file("/etc/ssl/certs/ca-certificates.crt")
jvm_extra = []
for trust in (Path("/etc/ssl/certs/java/cacerts"), Path(os.environ.get("JAVA_HOME", "/usr/lib/jvm/default-java")) / "lib/security/cacerts"):
    if trust.is_file():
        add_file(trust, "/trust/cacerts")
        jvm_extra = ["--host_jvm_args=-Djavax.net.ssl.trustStore=/trust/cacerts"]
        break
(root / "etc/passwd").write_text(f"runner:x:{os.getuid()}:{os.getgid()}:runner:/home/runner:/nonexistent\n")
(root / "etc/group").write_text(f"runner:x:{os.getgid()}:\n")
(root / "etc/nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files dns\n")
for directory in ("bin", "usr/bin", "work", "output", "repository-cache", "home/runner", "tmp"):
    (root / directory).mkdir(parents=True, exist_ok=True)
(root / "tmp").chmod(0o1777)
clients = {"8.8.1": "/probe/bazel"}
inventory = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
(evidence / "minimal-files.json").write_text(json.dumps(inventory, indent=2))
with (staging / "root.tar").open("wb") as f, tarfile.open(fileobj=f, mode="w") as tf:
    tf.add(root, arcname=".")
cmd("docker", "import", staging / "root.tar", "repo-fetch:local")
archive = staging / "source.tar"
cmd("git", "archive", "-o", archive, SOURCE)
def mounts(base, normal=False, helpers=()):
    args = ["docker", "run", "--rm", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,exec,mode=1777", "--user", f"{os.getuid()}:{os.getgid()}", "--workdir", "/work", "--env", "PATH=/usr/local/bin:/usr/bin:/bin", "--env", "HOME=/home/runner"]
    for local, dest in (("work", "/work"), ("output", "/output"), ("repository-cache", "/repository-cache"), ("home", "/home/runner")):
        args += ["--volume", f"{base/local}:{dest}"]
    for src, dest in helpers:
        args += ["--volume", f"{src}:{dest}:ro"]
    if normal:
        for directory in ("/usr", "/bin", "/lib"):
            args += ["--volume", f"{directory}:{directory}:ro"]
    return args + ["repo-fetch:local"]
def prepare(name):
    base = staging / name
    for folder in ("work", "output", "repository-cache", "home"):
        (base / folder).mkdir(parents=True)
    return base
def startup(version):
    return [clients[version], "--batch", "--nosystem_rc", "--nohome_rc", "--output_user_root=/output", *jvm_extra]

names=["ubuntu-tools","stock-bazel-only","bazel-lib-only","repaired-bazel-only"]
report={"source":SOURCE,"target":"//:release","client":"Linux ARM64/Bazel 8.8.1",
        "expected":names,"runs":{},"actions_expected":0,
        "older_small_target_evidence": "https://github.com/tamird/gvisor/actions/runs/41010936280"}
def save():
    (evidence/"verdict.json").write_text(json.dumps(report,indent=2)+"\n")
def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
def measure(name, restored=None):
    base=prepare(name)
    assert not any((base/"output").iterdir()) and not any((base/"repository-cache").iterdir())
    work=base/"work"
    cmd("tar","-xf",archive,"-C",work)
    assert not (work/'MODULE.bazel.lock').exists()
    if name == "bazel-lib-only" or name.startswith("repaired"):
        m=work/"MODULE.bazel"
        text=m.read_text()
        old='bazel_dep(name = "bazel_lib", version = "3.2.2")'
        assert text.count(old)==1 and 'module_name = "gazelle"' not in text
        m.write_text(text.replace(old,old.replace("3.2.2","3.4.0")))
    if name.startswith("repaired"):
        module_patch='\nsingle_version_override(module_name = "gazelle", patch_strip = 1, patches = ["//tools:scratch-client-gazelle.patch"])\n'
        m.write_text(m.read_text()+module_patch)
        shutil.copyfile(tree/"test/rbe/cold_client/gazelle.patch",work/"tools/scratch-client-gazelle.patch")
        with (work/"tools/BUILD").open("a") as f: f.write('\nexports_files(["scratch-client-gazelle.patch"])\n')
    receipt=base/"receipt";receipt.mkdir()
    helpers=[]
    if restored: helpers.append((restored,restored))
    args=mounts(base,normal=name=="ubuntu-tools",helpers=helpers)
    args=args[:-1]+["--volume",f"{receipt}:/receipt",args[-1]]
    options=["--nobuild","--config=aarch64","--compilation_mode=fastbuild",
             "--host_platform=//test/rbe/empty_worker:arm64","--extra_execution_platforms=//test/rbe/empty_worker:arm64",
             "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain","--extra_toolchains=//test/rbe/go_bootstrap:linux_arm64_ash",
             "--repository_cache=/repository-cache","--repo_env=PATH=/usr/local/bin:/usr/bin:/bin",
             "--jobs=12","--lockfile_mode=update","--color=no","--curses=no","--build_event_json_file=/receipt/bep","//:release"]
    row=report["runs"][name]={"module_sha256":digest((work/'MODULE.bazel').read_bytes()),"options":options,"cold":True,"from_lockfile":False,"start":now(),"restored":restored}
    save()
    try:
        p=subprocess.run(args+startup("8.8.1")+["build",*options],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=870)
        status,output=p.returncode,p.stdout
    except subprocess.TimeoutExpired as e:
        status,output=124,(e.stdout or b"")+b"\nprobe timeout"
    row["end"]=now()
    row["exit"]=status
    log=output.decode(errors="replace")
    # No repository or executor credentials are provided to this probe.
    # Retain the failing source lines, never action arguments or the full BEP.
    hints=("ERROR:", "Traceback (most recent call last)", "File \"", "Error in ", "Cannot run program","No such file or directory","command not found","not found in PATH","No executable","unknown repo","timed out","failed to execute","Failed to execute","Permission denied","PKIX","Error downloading","ERROR ")
    row["diagnostics"]=[v[:950] for v in log.splitlines() if any(h in v for h in hints) and not any(p in v.lower() for p in ("authorization", "credential", "api-key=", "token=", "header=", "bearer "))][:85]
    row["analyzed"]=next((l for l in log.splitlines() if "Analyzed target //:release " in l),None)
    row["zero_actions_line"]=next((l for l in log.splitlines() if "0 total actions" in l),None)
    bep=receipt/"bep"
    if bep.exists():
        for l in bep.open():
            try:e=json.loads(l)
            except ValueError:continue
            if "started" in e:row["invocation"]=e["started"].get("uuid")
            if "finished" in e:row["finish"]=e["finished"].get("exitCode")
            if "buildMetrics" in e:row["spawn_count"]=int(e["buildMetrics"].get("actionSummary",{}).get("actionsExecuted",0))
    lock=work/'MODULE.bazel.lock'
    if lock.exists():row["lock_sha256"]=digest(lock.read_bytes())
    row["analysis_passed"]=status==0 and bool(row["analyzed"]) and int(row.get("spawn_count",0))==0 and row.get("finish",{}).get("name")=="SUCCESS"
    row["original_diagnostic"]=status not in (0,124) and bool(row["diagnostics"]) and bool(row.get("finish"))
    save()
    print(name,status,row.get("analyzed") or row["diagnostics"][-3:],flush=True)
    shutil.rmtree(base)
    return row
for n in names:measure(n)
last=report["runs"][names[-1]]
if last["original_diagnostic"]:
    # When Bazel reports a literal missing command, restore only that one
    # executable on a fourth cold client and preserve the next result.
    diags="\n".join(last["diagnostics"])
    whitelist=("env","uname","bash","sh","patch","cp","git","which","realpath","readlink","tar","mkdir","ln","sed","ls","cat","python3","touch","ar")
    candidate=next((x for x in whitelist if re.search(r'Cannot run program "(?:[^"]*/)?'+x+r'"|(?:^|[ :])(?:/usr)?/bin/'+x+r': (?:No such file or directory|not found)|[\s\'\"]'+x+r'[\'\"]? (?:is not found|not found) in (?:host )?PATH',diags,re.M)),None)
    if candidate and (path:=shutil.which(candidate)):
        requested=re.search(r'Cannot run program "((?:/usr)?/bin/'+candidate+r')"' ,diags)
        if requested and Path(requested[1]).is_file():path=requested[1]
        name="repaired-plus-"+candidate
        report["expected"].append(name)
        measure(name,path)
report["completed_originals"]=sum(r["analysis_passed"] or r["original_diagnostic"] for r in report["runs"].values())
report["evidence_complete"]=report["expected"]==list(report["runs"]) and report["runs"]["ubuntu-tools"]["analysis_passed"] and report["completed_originals"]==len(report["expected"])
save()
raise SystemExit(not report["evidence_complete"])
