"""Compare Bazel repository queries in fresh Linux roots on the same runner."""

import gzip
import hashlib
import io
import sys
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

SOURCE = "f8c112a4db369c70c88857cb13be54f537468664"
LOCK = "c69205c783c538f252750fb21bcbabb49495b36d638ba19b7b3ee30cdbae795b"
BAZEL = "5b5bab2095065817d3416ca9a0678bd39c10c9ede432dbe8187bfe74de285160"
tree = Path.cwd()
staging = Path(tempfile.mkdtemp(dir=os.environ["RUNNER_TEMP"], prefix="repo-fetch-"))
evidence = tree / "repo-fetch-evidence"
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
shutil.copy2(Path(os.environ["RUNNER_TEMP"])/"bazel-candidate", client)
client.chmod(0o755)
add_file(client, "/probe/bazel")
# The Bazel native launcher and its embedded JDK are explicitly allowed.
# Include shared libraries only; copying whole /usr/lib would also supply git.
for soname in ("ld-linux-x86-64.so.2", "libc.so.6", "libm.so.6", "libz.so.1",
               "libdl.so.2", "libpthread.so.0", "libgcc_s.so.1", "libstdc++.so.6",
               "libresolv.so.2", "librt.so.1", "libutil.so.1", "libnss_files.so.2",
               "libnss_dns.so.2", "libtinfo.so.6", "libcrypt.so.1", "libzstd.so.1"):
    src = Path("/lib/x86_64-linux-gnu") / soname
    if src.exists():
        add_file(src)
add_file("/lib64/ld-linux-x86-64.so.2")
add_file("/etc/ssl/certs/ca-certificates.crt")
# Bazel's repository extractor needs the coordinator's UTF-8 character tables
# for the Go archive's Þfoo.go and Þmain.go. These are runtime data only.
locale = Path("/usr/lib/locale/C.utf8")
assert (locale / "LC_CTYPE").is_file()
for table in locale.rglob("*"):
    if table.is_file():
        add_file(table)
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
# Fetch the fixed input itself instead of relying on the carrier's clone depth.
cmd("git", "fetch", "--no-tags", "--depth=1", "https://github.com/tamird/gvisor.git", SOURCE)
cmd("git", "archive", "-o", archive, SOURCE)
lock = gzip.decompress((tree / "test/rbe/repo_fetch_lock.gz").read_bytes())
assert digest(lock) == LOCK
outcomes = {}
def mounts(base, normal=False, helpers=()):
    args = ["docker", "run", "--rm", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,exec,mode=1777", "--user", f"{os.getuid()}:{os.getgid()}", "--workdir", "/work", "--env", "LC_ALL=C.UTF-8", "--env", "LANG=C.UTF-8", "--env", "PATH=/usr/local/bin:/usr/bin:/bin", "--env", "HOME=/home/runner"]
    for local, dest in (("work", "/work"), ("output", "/output"), ("repository-cache", "/repository-cache"), ("home", "/home/runner")):
        args += ["--volume", f"{base/local}:{dest}"]
    for src, dest in helpers:
        args += ["--volume", f"{src}:{dest}:ro"]
    if normal:
        for directory in ("/usr", "/bin", "/lib", "/lib64"):
            args += ["--volume", f"{directory}:{directory}:ro"]
    return args + ["repo-fetch:local"]
def prepare(name):
    base = staging / name
    for folder in ("work", "output", "repository-cache", "home"):
        (base / folder).mkdir(parents=True)
    return base
def run(name, args, timeout=600, **kwargs):
    try:
        res = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, **kwargs)
        output, status = res.stdout, res.returncode
    except subprocess.TimeoutExpired as exc:
        output, status = (exc.stdout or b"") + b"\nPROBE TIMEOUT\n", 124
    log = output.decode(errors="replace")
    (evidence/f"{name}.log").write_text(log)
    outcomes[name] = status
    print(name, status, log[-900:], flush=True)
    return status
def startup(version):
    return [clients[version], "--batch", "--nosystem_rc", "--nohome_rc", "--output_user_root=/output", *jvm_extra]
name = "actual-go-build"
base = prepare(name)
receipt = base / "receipt"; receipt.mkdir()
cmd("tar", "-xf", archive, "-C", base/"work")
work = base / "work"
(work/"MODULE.bazel.lock").write_bytes(lock)
shutil.copyfile(tree/"test/rbe/repo_fetch_env.patch", work/"tools/gazelle-native-environment.patch")
m = work/"MODULE.bazel"
s = m.read_text()
for old,new in [('patches = ["//tools:gazelle-module-metadata.patch"],','patches = ["//tools:gazelle-module-metadata.patch", "//tools:gazelle-native-environment.patch"],'), ('bazel_dep(name = "bazel_lib", version = "3.2.2")','bazel_dep(name = "bazel_lib", version = "3.4.0")')]:
    assert s.count(old)==1
    s=s.replace(old,new)
m.write_text(s)
b=work/"tools/BUILD"; old='    "gazelle-module-metadata.patch",';s=b.read_text()
assert s.count(old)==1;b.write_text(s.replace(old,old+'\n    "gazelle-native-environment.patch",'))
secret = os.environ.pop("BUILDBUDDY_API_KEY")
assert secret and "\n" not in secret
auth_path=base/"release.bazelrc"
auth_path.write_text("\n".join([
    "build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io",
    "build:buildbuddy_remote_executor --remote_cache=grpcs://remote.buildbuddy.io",
    "build:buildbuddy_remote_executor --bes_backend=grpcs://remote.buildbuddy.io",
    "build:buildbuddy_remote_executor --bes_results_url=https://app.buildbuddy.io/invocation/",
    "build:buildbuddy_remote_executor --remote_header=x-buildbuddy-api-key="+secret,
    "build:buildbuddy_remote_executor --bes_header=x-buildbuddy-api-key="+secret,
])+"\n")
auth_path.chmod(0o400)
# read-only binary/root and credential mounts; the receipt directory must be writable.
args=mounts(base,helpers=((str(auth_path),"/auth/release.bazelrc"),))
args=args[:-1]+["--volume",f"{receipt}:/receipt",args[-1]]
# The direct action is a qualification control, not a proposed product test rule.
target="//pkg/sentry/fsimpl/eventfd:eventfd_test";control="//pkg/sentry/fsimpl/eventfd:scratch_eventfd"
# Bazel 8 propagated gVisor's Starlark build options into exec configurations.
# Its declared Go bootstrap interpreter must remain selected for tool builds.
common=["--noincompatible_exclude_starlark_flags_from_exec_config","--workspace_status_command=","--config=aarch64","--config=rbe-arm64","--jobs=32","--noremote_local_fallback","--spawn_strategy=remote","--repository_cache=/repository-cache","--lockfile_mode=update","--repo_env=PATH=/usr/local/bin:/usr/bin:/bin","--host_platform=//tools/bazeldefs:rbe_linux_arm64","--extra_execution_platforms=//tools/bazeldefs:rbe_linux_arm64","--remote_download_outputs=toplevel","--remote_download_regex=.*pkg/sentry/fsimpl/eventfd/.*"]
client_prefix=args+startup("8.8.1")+["--bazelrc=/auth/release.bazelrc"]
redact=lambda txt:txt.replace(secret,"[redacted]")
decoder=staging/"bb";cmd("curl","-fsSL","--retry","3","-o",decoder,"https://github.com/buildbuddy-io/bazel/releases/download/5.0.488/bazel-5.0.488-linux-x86_64")
assert digest(decoder.read_bytes())=="f0d85b1514f784dc25705dd3bbeeaa1e603b5449f721c26add0245198f03e7cb";decoder.chmod(0o755)
sys.path.insert(0,str(work/"test/rbe"))
from qualification_report import compact_records,spawn_output_files,input_chain
import xml.etree.ElementTree as ET
expected_image="docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
expected_cases={"TestEventFD","TestEventFDStat"};report={"source":SOURCE,"target":target,"entrypoint":"bazel test","lock":LOCK,"image":expected_image,"coordinator":inventory}
for arm,command,targets in [("native","test",["--incompatible_use_native_posix_test_wrapper","--nocache_test_results","--features=fully_static_link","--nobuild_event_json_file_path_conversion",target])]:
    event_file=receipt/(arm+".events");action_file=receipt/(arm+".actions")
    argv=client_prefix+[command]+common+[f"--execution_log_compact_file=/receipt/{arm}.actions",f"--build_event_json_file=/receipt/{arm}.events"]+targets
    try:
        res=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=1350)
        raw=res.stdout;rc=res.returncode
    except subprocess.TimeoutExpired as exc:
        rc=124;raw=(exc.stdout or b"")+b"\nQUALIFICATION TIMEOUT\n"
    log=redact(raw.decode(errors="replace"));(evidence/(arm+".log")).write_text(log)
    print(arm,rc,log[-1700:],flush=True)
    result={"exit":rc,"invocation":"","cases":[],"owner_spawns":[],"failures":[],"cached_other_images":[],"noncached_wrong_platform":[]}
    report[arm]=result
    if event_file.exists():
        for line in event_file.read_text().splitlines():
            ev=json.loads(line)
            if "started" in ev: result["invocation"]=ev["started"]["uuid"]
            if "testSummary" in ev: result["test_summary"]=ev["testSummary"].get("overallStatus")
            if "aborted" in ev and ev["aborted"].get("description"):
                result.setdefault("aborted",[]).append(redact(ev["aborted"]["description"][:400]))
    if action_file.exists() and action_file.stat().st_size:
        decoded=staging/(arm+".records")
        with decoded.open("wb") as f:cmd(decoder,"print",f"--compact_execution_log={action_file}","--raw=true","--sort=false",stdout=f)
        records,spawns=compact_records(decoded); count=Counter()
        for spawn in spawns:
            mnemonic=spawn.get("mnemonic","");label=spawn.get("targetLabel","");props={p["name"]:p["value"] for p in spawn.get("platform",{}).get("properties",[])}
            runner=spawn.get("runner","");image=props.get("container-image","");arch=props.get("Arch","")
            cached=bool(spawn.get("cacheHit") or spawn.get("remoteCacheHit") or "cache" in runner.lower())
            item=dict(mnemonic=mnemonic,label=label,runner=runner,arch=arch,image=image,cached=cached,action=spawn.get("digest"),exit=spawn.get("exitCode",0),status=spawn.get("status",""))
            count[(mnemonic,"cached" if cached else "ran")]+=1
            if item["exit"] or item["status"]:result["failures"].append(item)
            if not cached and (runner!="remote" or arch!="arm64" or image!=expected_image): result["noncached_wrong_platform"].append(item)
            if mnemonic not in ("TestRunner","ScratchEventfdTest","GoLink") or label not in [target,control]:continue
            if mnemonic=="TestRunner":
                item["command_program"]=spawn.get("args",[])[:1]
            if mnemonic=="ScratchEventfdTest":
                item["arguments"]=spawn.get("args",[])[:1]
                matches=[(i,v["file"]) for i,v in records.items() if v.get("file",{}).get("path","").endswith("/eventfd_test_/eventfd_test")]
                item["programs"]=[{"path":v["path"],"sha256":v["digest"]["hash"],"in_input_tree":input_chain(records,spawn["inputSetId"],i) is not None} for i,v in matches]
            item["outputs"]=[]
            if mnemonic=="TestRunner":item["arguments"]=[redact(x) for x in spawn.get("args",[])[:4]]
            for path,meta in spawn_output_files(records,spawn).items():
                if not any([path.endswith("scratch_eventfd.xml"),path.endswith("/eventfd_test"),path.endswith("/test.xml"),path.endswith("/test.log")]):continue
                files=list((base/"output").glob("*/execroot/*/"+path))
                if len(files)!=1 or not files[0].is_file():continue
                data=files[0].read_bytes();obj={"path":path,**meta,"matches":digest(data)==meta["sha256"] and len(data)==meta["bytes"]}
                if data.startswith(b"\x7fELF"):obj["elf_machine"]=int.from_bytes(data[18:20],"little")
                if path.endswith(".xml"):
                    (evidence/(arm+"-package.xml")).write_bytes(data)
                    try:
                        cases=[(x.get("name",""),[y.tag for y in x if y.tag in ("skipped","failure","error")]) for x in ET.fromstring(data).iter("testcase")]
                        result["cases"]=cases
                    except ET.ParseError: pass
                if path.endswith("/test.log"):(evidence/(arm+"-test.log")).write_text(redact(data.decode(errors="replace")))
                item["outputs"].append(obj)
            result["owner_spawns"].append(item)
        result["spawns"]=len(spawns);result["counts"]=[[*k,n] for k,n in sorted(count.items())]
    (evidence/"eventfd.json").write_text(json.dumps(report,indent=2))

x=report["native"]
print("normal entrypoint", x["exit"], x["cases"], x["owner_spawns"], flush=True)
assert x["exit"]==0 and not x["noncached_wrong_platform"] and x["owner_spawns"],x
assert sorted(p[0] for p in x["cases"] if not p[1])==sorted(expected_cases),x["cases"]
assert any(q.get("matches") and q["path"].endswith("/test.xml") for o in x["owner_spawns"] for q in o["outputs"]),x
assert any(o["mnemonic"]=="TestRunner" and o["runner"]=="remote" and o["arch"]=="arm64" and not o["cached"] and not o["exit"] for o in x["owner_spawns"]),x
