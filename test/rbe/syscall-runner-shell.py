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
import time
from datetime import datetime, timezone

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
cmd("git", "fetch", "--no-tags", "--depth=1", "https://github.com/tamird/gvisor.git", SOURCE, "7317e2f45ed1410ff688ebaaf9c0e4ff1c7ce6e9", "cbdbdb82b0eb2aa95fa97d38df19dec2db5fb7e5")
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
# Use #16506's already-qualified executable layout in both arms.
# Its two input blobs are identical on this frozen release integration.
layout_patch=tree / 'test/rbe/syscall-runner-release-files.patch'
cmd("git","apply","--check",str(layout_patch),cwd=work)
cmd("git","apply",str(layout_patch),cwd=work)
layout_files={'tools/BUILD': '5ea6a82cfd80530f60a09cfa6790b1997c915d1ac31d7baf17f2fe18901dd5d2', 'tools/release.bzl': '69a0fa210e8e39552ec3a06218ed90d395cc2f183106ee58738a829633809259'}
assert all(digest((work/p).read_bytes())==expected for p, expected in layout_files.items())
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
# The normal consumer sets this option on the command line. One configured
# rules_go builder nevertheless resolves it to the supplier's default on Bazel
# HEAD. Keep the selected Go SDK/interpreter identical in that context too.
goboot = work/"tools/rules_go_bootstrap_shell.patch"
extra = """diff --git a/go/config/BUILD.bazel b/go/config/BUILD.bazel
--- a/go/config/BUILD.bazel
+++ b/go/config/BUILD.bazel
@@ -43,5 +43,5 @@
 bool_flag(
     name = "experimental_use_sh_toolchain",
-    build_setting_default = False,
+    build_setting_default = True,
     visibility = ["//visibility:public"],
 )
"""
goboot.write_text(goboot.read_text()+extra)
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
target="//test/syscalls:eventfd_test_native";control="//test/rbe/syscall_runner_contract:eventfd_test_native"
# Bazel 8 propagated gVisor's Starlark build options into exec configurations.
# Its declared Go bootstrap interpreter must remain selected for tool builds.
common=["--noincompatible_exclude_starlark_flags_from_exec_config","--workspace_status_command=","--config=aarch64","--config=rbe-arm64","--jobs=32","--noremote_local_fallback","--spawn_strategy=remote","--repository_cache=/repository-cache","--lockfile_mode=update","--repo_env=PATH=/usr/local/bin:/usr/bin:/bin","--host_platform=//tools/bazeldefs:rbe_linux_arm64","--extra_execution_platforms=//tools/bazeldefs:rbe_linux_arm64","--remote_download_outputs=toplevel","--remote_download_regex=.*/testlogs/test/(syscalls|rbe/syscall_runner_contract)/eventfd_test_native/.*|.*/bin/test/(syscalls/eventfd_test_native|runner/setup_container/setup_container)$"]
client_prefix=args+startup("8.8.1")+["--bazelrc=/auth/release.bazelrc"]
redact=lambda txt:txt.replace(secret,"[redacted]")
decoder=staging/"bb";cmd("curl","-fsSL","--retry","3","-o",decoder,"https://github.com/buildbuddy-io/bazel/releases/download/5.0.488/bazel-5.0.488-linux-x86_64")
assert digest(decoder.read_bytes())=="f0d85b1514f784dc25705dd3bbeeaa1e603b5449f721c26add0245198f03e7cb";decoder.chmod(0o755)
sys.path.insert(0,str(work/"test/rbe"))
from qualification_report import compact_records,spawn_output_files,input_chain
import xml.etree.ElementTree as ET
expected_image="docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
original="7317e2f45ed1410ff688ebaaf9c0e4ff1c7ce6e9"
corrected="cbdbdb82b0eb2aa95fa97d38df19dec2db5fb7e5"
paths=["test/runner","test/syscalls/BUILD"]
names = {"Nonblock","BlockingWrite","SmallWrite","SmallRead","IllegalSeek","IllegalPread","IllegalPwrite","BigWriteFails","BigRead","NotifyNonZero","SpliceReturnsEINVAL"}
report={"release_layout":'b1cb68d647deac35165b79f0ec5c230c12e4e562',"release_file_sources":layout_files,"build_integration":SOURCE,"stock":original,"corrected":corrected,"exact_source_paths":paths,"target":target,"entrypoint":"bazel test","lock":LOCK,"image":expected_image,"coordinator":inventory}
def make_static(build, name):
    marker='    name = "'+name+'",'
    text=build.read_text()
    assert text.count(marker)==1,(name,str(build))
    feature=marker+'\n    features = ["fully_static_link"],'
    if feature not in text:
        build.write_text(text.replace(marker,feature))

# Keep the two test executables standalone. A global fully_static_link would
# also impose -static on both freestanding architecture transitions in //:release.
make_static(work/"test/syscalls/linux/BUILD", "eventfd_test")
for arm, revision in [("original",original),("corrected",corrected)]:
    # Use the matched supplier/toolchain assembly, but the current gVisor test
    # rule, actual runner and setup helper, and normal public target definition.
    with tarfile.open(fileobj=io.BytesIO(cmd("git","archive",revision,"--",*paths,stdout=subprocess.PIPE).stdout)) as package:
        package.extractall(work, filter="data")
    make_static(work/"test/runner/setup_container/BUILD","setup_container")
    if arm=="original":
        # Inspect every affected configured link before submitting a test action.
        for platform_label in ["rbe_linux_arm64"]:
            log = "aquery-"+platform_label
            query='outputs(".*(vdso/vdso.so|sysmsg/sighandler.built-in.bin.o|test/syscalls/linux/eventfd_test|test/runner/setup_container/setup_container|tools/test/compiled_posix_test_wrapper|release/[^/]+|release/gvisor-bin/[^/]+)$", mnemonic("CppLink|CopyFile|ReleaseFiles", deps('+target+')))'
            query_args=client_prefix+["aquery"]+common+["--platforms=//tools/bazeldefs:"+platform_label,"--incompatible_use_native_posix_test_wrapper","--include_param_files","--output=jsonproto",query]
            proc=subprocess.run(query_args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=800)
            (evidence/(log+".log")).write_text(redact(proc.stderr.decode(errors="replace"))[-40000:])
            src=json.loads(proc.stdout) if proc.returncode==0 else {}
            labels={str(x["id"]):x["label"] for x in src.get("targets",[])}
            entries=[]
            copies=[]
            dep_sets={str(d["id"]):d for d in src.get("depSetOfFiles",[])}
            artifacts={str(a["id"]):str(a["pathFragmentId"]) for a in src.get("artifacts",[])}
            fragments={str(f["id"]):f for f in src.get("pathFragments",[])}
            def input_paths(action):
                pending=list(map(str,action.get("inputDepSetIds",[])))
                visited=set()
                paths=set()
                while pending:
                    dep_id=pending.pop()
                    if dep_id in visited: continue
                    visited.add(dep_id)
                    dep=dep_sets[dep_id]
                    pending.extend(map(str,dep.get("transitiveDepSetIds",[])))
                    for file_id in dep.get("directArtifactIds",[]):
                        frag=fragments[artifacts[str(file_id)]]
                        names=[frag["label"]]
                        while "parentId" in frag and str(frag["parentId"]) in fragments:
                            frag=fragments[str(frag["parentId"])]
                            names.append(frag["label"])
                        paths.add("/".join(reversed(names)))
                return paths
            for action in src.get("actions",[]):
                args=[*action.get("arguments",[])]
                if labels.get(str(action.get("targetId")))=="//:release":
                    copies.append({"mnemonic":action["mnemonic"],"argv":args,"executable_declared":bool(args and args[0] in input_paths(action))})
                    continue
                for param in action.get("paramFiles",[]): args.extend(param.get("arguments",[]))
                entries.append({"target":labels.get(str(action.get("targetId"))), "argv":[x for x in args if (any(y in x for y in ("musl","glibc","-pthread","-lpthread","-ldl","-nostdlib","-static","-target","--target=","--sysroot")))]})
            (evidence/(log+".json")).write_text(json.dumps({"exit":proc.returncode,"links":entries,"release_files":copies},indent=2))
            print(log,proc.returncode,[(e["target"],e["argv"][:3]) for e in entries],flush=True)
        assert proc.returncode==0 and entries, (proc.returncode, entries)
        expected_layout=['containerd-shim-runsc-v1', 'gvisor-bin/checkpointgofer', 'gvisor-bin/gvisor-proc-umounter', 'gvisor-bin/gvisor-sentry-prewarmer', 'gvisor-bin/gvisor_sentry', 'gvisor-bin/runsc-fd-parking', 'gvisor-bin/runsc-metric-server', 'runsc']
        assert copies and all(c["mnemonic"]=="CopyFile" and c["executable_declared"] and c["argv"][1]=="cp" and not c["argv"][0].startswith("/") for c in copies),copies
        assert sorted({c["argv"][-1].split("/bin/release/")[-1] for c in copies})==expected_layout,copies
        for label in ("//vdso:vdso.so","//pkg/sentry/platform/systrap/sysmsg:sighandler.built-in.bin.o"):
            relevant=[e["argv"] for e in entries if e["target"]==label]
            assert len(relevant)==2 and all("-nostdlib" in a and "-static" not in a for a in relevant),(label,relevant)
        for label in ("//test/syscalls/linux:eventfd_test","//test/runner/setup_container:setup_container"):
            relevant=[e["argv"] for e in entries if e["target"]==label]
            assert relevant and all("-static" in a and any("musl_library_search_directory" in f for f in a) for a in relevant),(label,relevant)
        common+=["--platforms=//tools/bazeldefs:rbe_linux_arm64"]
    required_sources = {p:digest((work/p).read_bytes()) for p in ["test/runner/defs.bzl","test/runner/main.go","test/runner/setup_container/setup_container.cc","test/runner/BUILD","test/syscalls/BUILD","test/syscalls/linux/eventfd.cc"]}
    command="test"
    targets=["--keep_going","--verbose_failures","--incompatible_use_native_posix_test_wrapper","--remote_accept_cached","--nocache_test_results","--test_output=all","--test_arg=--debug=false","--nobuild_event_json_file_path_conversion",target]

    event_file=receipt/(arm+".events");action_file=receipt/(arm+".actions")
    argv=client_prefix+[command]+common+[f"--execution_log_compact_file=/receipt/{arm}.actions",f"--build_event_json_file=/receipt/{arm}.events"]+targets
    command_started=time.time()
    try:
        res=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=2400)
        raw=res.stdout;rc=res.returncode
    except subprocess.TimeoutExpired as exc:
        rc=124;raw=(exc.stdout or b"")+b"\nQUALIFICATION TIMEOUT\n"
    command_finished=time.time()
    log=redact(raw.decode(errors="replace"));(evidence/(arm+".log")).write_text(log)
    print(arm,rc,log[-1700:],flush=True)
    result={"exit":rc,"invocation":"","cases":[],"owner_spawns":[],"failures":[],"cached_other_images":[],"noncached_wrong_platform":[]}
    result["source_files"]=required_sources
    result["bazel_started_epoch"]=command_started;result["bazel_exited_epoch"]=command_finished
    result["test_results"]=[]
    result["target_platform"]="//tools/bazeldefs:rbe_linux_arm64"
    report[arm]=result
    if event_file.exists():
        for line in event_file.read_text().splitlines():
            ev=json.loads(line)
            if "started" in ev: result["invocation"]=ev["started"]["uuid"]
            if ev.get("id",{}).get("testResult",{}).get("label")==target and "testResult" in ev:
                tr=ev["testResult"]
                result["test_results"].append({k:v for k,v in tr.items() if k in ("status","testAttemptStartMillisEpoch","testAttemptDurationMillis","testAttemptStart","testAttemptDuration","executionInfo","cachedLocally")})
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
            if item["exit"] or item["status"]:
                item["argv"]=[redact(x) for x in spawn.get("args",[])[:20]]
                result["failures"].append(item)
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
                if path.endswith("/test.log"):(evidence/(arm+("-test.log" if label==target else "-custom-test.log"))).write_text(redact(data.decode(errors="replace")))
                item["outputs"].append(obj)
            result["owner_spawns"].append(item)
        result["spawns"]=len(spawns);result["counts"]=[[*k,n] for k,n in sorted(count.items())]
    launcher = list((base/"output").glob("*/execroot/*/bazel-out/*/bin/test/syscalls/eventfd_test_native"))
    result["launchers"]=[]
    for launcher_file in launcher:
        if launcher_file.is_file():
            payload=launcher_file.read_bytes()
            def interpreter(data):
                import struct
                if not data.startswith(b"\x7fELF") or data[4:6]!=b"\x02\x01": return None
                headers=struct.unpack_from("<Q",data,32)[0];size,count=struct.unpack_from("<HH",data,54)
                for n in range(count):
                    hdr=headers+n*size
                    if struct.unpack_from("<I",data,hdr)[0]==3:
                        pos=struct.unpack_from("<Q",data,hdr+8)[0]
                        return data[pos:data.index(b"\0",pos)].decode()
                return None
            result["launchers"].append({"interpreter":interpreter(payload),"path":str(launcher_file.relative_to(base/"output")),"sha256":digest(payload),"size":len(payload),"head":payload[:64].hex(),"shebang":payload.split(b"\n",1)[0].decode(errors="replace") if payload.startswith(b"#!") else None,"elf_machine":int.from_bytes(payload[18:20],"little") if payload.startswith(b"\x7fELF") else None})
    folder = "bazel-out/*/testlogs/test/syscalls/eventfd_test_native"
    for filename in ("test.log","test.xml"):
        matches=list((base/"output").glob("*/execroot/*/"+folder+"/"+filename))
        if len(matches)==1 and matches[0].is_file():
            data=matches[0].read_bytes()
            (evidence/(arm+"-"+filename)).write_bytes(data)
            result["local_"+filename]={"sha256":digest(data),"bytes":len(data)}
    (evidence/"eventfd.json").write_text(json.dumps(report,indent=2))


def actual_runner(arm,label=target):
    return [s for s in report[arm]["owner_spawns"] if s["label"]==label and s["mnemonic"]=="TestRunner" and s["runner"]=="remote" and s["arch"]=="arm64" and s["image"]==expected_image and not s["cached"]]

def testlog(arm):
    path=evidence/(arm+"-test.log")
    if path.is_file() and path.stat().st_size:
        report[arm]["log_source"]="Bazel test.log"
        return path.read_text(errors="replace")
    report[arm]["log_source"]="Bazel --test_output=all"
    return (evidence/(arm+".log")).read_text(errors="replace")

def stamp(number, rfc):
    if number is not None: return float(number)/1000
    if rfc: return datetime.fromisoformat(rfc.replace("Z","+00:00")).timestamp()
    return None
verdict_time=time.time()
for arm in ("original","corrected"):
    data=report[arm]
    finishes=[]
    for tr in data["test_results"]:
        ts=stamp(tr.get("testAttemptStartMillisEpoch"),tr.get("testAttemptStart"))
        duration=tr.get("testAttemptDurationMillis")
        if duration is not None: duration=float(duration)/1000
        elif tr.get("testAttemptDuration"): duration=float(tr["testAttemptDuration"].removesuffix("s"))
        if ts is not None and duration is not None: finishes.append(ts+duration)
    data["last_test_attempt_finish_epoch"]=max(finishes) if finishes else None
    data["last_test_finish_to_verdict_seconds"]=verdict_time-max(finishes) if finishes else None
    data["bazel_exit_to_verdict_seconds"]=verdict_time-data["bazel_exited_epoch"]
report["verdict_epoch"]=verdict_time
(evidence/"eventfd.json").write_text(json.dumps(report,indent=2))

original, revised = report["original"],report["corrected"]
stocklog=testlog("original")
fixedlog=testlog("corrected")
assert original["exit"] and actual_runner("original") and not original["noncached_wrong_platform"],original
assert any(x["shebang"]=="#!/bin/bash" for x in original["launchers"]),original["launchers"]
assert ("No such file or directory" in stocklog or "ENOENT" in stocklog) and "eventfd_test_native" in stocklog,stocklog
assert not any("[ RUN      ] EventfdTest."+n in stocklog for n in names),stocklog
assert revised["exit"]==0 and actual_runner("corrected") and not revised["noncached_wrong_platform"],revised
assert any(x["elf_machine"]==183 and x["interpreter"] is None for x in revised["launchers"]),revised["launchers"]
passed={n for n in names if "[       OK ] EventfdTest."+n in fixedlog}
skipped={n for n in names if "[  SKIPPED ] EventfdTest."+n in fixedlog}
assert all("[ RUN      ] EventfdTest."+n in fixedlog for n in names),fixedlog
assert passed | skipped == names and not passed & skipped, (passed,skipped)
assert {"IllegalSeek","SpliceReturnsEINVAL"} <= skipped <= {"IllegalSeek","SpliceReturnsEINVAL","BigWriteFails"},(passed,skipped)
assert "[  FAILED  ] EventfdTest." not in fixedlog,fixedlog
revised["passed"]=sorted(passed);revised["skipped"]=sorted(skipped)
(evidence/"eventfd.json").write_text(json.dumps(report,indent=2))
print("Original remote Bash shebang failed. Corrected native cases passed:",sorted(passed),"skipped:",sorted(skipped))
