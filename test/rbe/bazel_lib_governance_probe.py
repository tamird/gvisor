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

SOURCE = "cd90d7a7403ed4a739b2cf109463b0e76270b87a"
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
cmd("curl", "-fsSL", "--retry", "3", "-o", client, "https://releases.bazel.build/8.8.1/release/bazel-8.8.1-linux-x86_64")
assert digest(client.read_bytes()) == BAZEL
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
target="@governance_areas//:areas.yaml"
report={}
for arm in ["stock", "updated"]:
    base=prepare(arm);work=base/"work"
    cmd("tar","-xf",archive,"-C",work)
    m=work/"MODULE.bazel"
    b=m.read_bytes();original=b'bazel_dep(name = "bazel_lib", version = "3.2.2")'
    assert b.count(original)==1
    if arm=="updated": m.write_bytes(b.replace(original,original.replace(b"3.2.2",b"3.4.0")))
    args=mounts(base)+startup("8.8.1")+["query","--repository_cache=/repository-cache","--lockfile_mode=update","--repo_env=PATH=/usr/local/bin:/usr/bin:/bin",target]
    res=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=600)
    log=res.stdout.decode(errors="replace")
    (evidence/(arm+".log")).write_text(log)
    links=[]
    for out in (base/"output").glob("*/external/*governance_areas/areas.yaml"):
        if not out.is_symlink():continue
        dest=os.readlink(out)
        if dest=="/work/governance/areas.yaml": actual=work/"governance/areas.yaml"
        elif dest.startswith("/"):continue
        else:actual=out.parent/dest
        if actual.is_file():links.append({"target":dest,"matches":actual.read_bytes()==(work/"governance/areas.yaml").read_bytes(),"sha256":digest(actual.read_bytes())})
    report[arm]={"rc":res.returncode,"selected":target in log,"matching_outputs":links}
    (evidence/"outcomes.json").write_text(json.dumps(report,indent=2))
    print(arm,res.returncode,log[-1500:])
assert report["stock"]["rc"]!=0 and report["updated"]["rc"]==0 and report["updated"]["selected"] and any(l["matches"] for l in report["updated"]["matching_outputs"]),report
