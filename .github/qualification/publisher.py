"""Exercise the staged release commands on Linux with public inputs and a disposable key."""

import collections
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

BASE = "9ee2906f518e1d0f9fd109e9e1feb2812096418b"
PRODUCT = "c661271ae7b13f9e607958444e8cbf553c883720"
W = Path(os.environ["PUBLICATION_FIXTURE"])
T = Path(os.environ["TEST_TMPDIR"])
SOURCE = W / "source"
TREE = T / "checkout"
STAGE = TREE / "artifacts"
DEST = TREE / "repo"
VERIFY = T / "verify"
REPORT = W / "public/publication.json"
DOC = {"base": BASE, "product": PRODUCT, "arms": {}, "verified": False}
CPUS = {"x86_64": "amd64", "aarch64": "arm64"}
SUFFIXES = ("bz2", "zstd")


def run(*args, cwd=None, check=True, content=None):
    return subprocess.run(args, cwd=cwd or TREE, input=content, check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def hash_file(path):
    with path.open("rb") as stream:
        h = hashlib.sha256()
        while block := stream.read(1024*1024):
            h.update(block)
        return h.hexdigest()


def save():
    value = json.dumps(DOC, indent=2) + "\n"
    assert len(value) < 65_000
    REPORT.write_text(value)


def git(*args):
    return run("git", *args).stdout.decode().strip()


def prepare():
    TREE.mkdir()
    git("init", "-q")
    git("-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-tags", "--depth=2",
        SOURCE.as_uri(), PRODUCT)
    git("checkout", "-q", "--detach", PRODUCT)
    assert git("rev-parse","HEAD^") == BASE
    git("config", "url." + (TREE/".git").as_uri() + ".insteadOf", "https://github.com/google/gvisor.git")
    shim = T / "bin"
    shim.mkdir()
    for name in ("docker", "bazel", "gcloud", "twine", "pip", "pip3"):
        p = shim / name
        p.write_text("#!/bin/bash\n" + r'''printf '%s\0' "$@" >> "$TEST_TMPDIR/trace-@NAME@"
printf '\0' >> "$TEST_TMPDIR/trace-@NAME@"
[[ "@NAME@" == twine && "$1" == upload ]] || exit 125
'''.replace("@NAME@", name))
        p.chmod(0o755)
    os.environ["PATH"] = str(shim) + ":/usr/bin:/bin"
    # The real release helper prepends these two directories for twine.
    assert shutil.which("twine", path=os.environ["HOME"]+"/.local/bin:/usr/local/bin:"+os.environ["PATH"]) == str(shim/"twine")
    os.environ["GNUPGHOME"] = str(T/"generate")
    Path(os.environ["GNUPGHOME"]).mkdir(mode=0o700)
    VERIFY.mkdir(mode=0o700)
    for cpu, deb_cpu in CPUS.items():
        dest = STAGE / cpu
        dest.mkdir(parents=True)
        for kind in SUFFIXES:
            shutil.copyfile(W/"input"/cpu/("gvisor.tar."+kind), dest/("gvisor.tar."+kind))
        signed = W/"input"/cpu/"runsc.deb"
        deb = dest/"runsc.deb"
        shutil.copyfile(signed, deb)
        names = run("ar","t",deb).stdout.splitlines()
        assert names == [b"debian-binary",b"control.tar.gz",b"data.tar.gz",b"_gpgbuilder"]
        run("ar","d",deb,"_gpgbuilder")
        assert run("ar","t",deb).stdout.splitlines() == names[:-1]
        for member in names[:-1]:
            assert run("ar","p",deb,member).stdout == run("ar","p",signed,member).stdout
        assert run("dpkg-deb","-f",deb,"Architecture").stdout.decode().strip() == deb_cpu
        (dest/"runsc.changes").write_text("Format: 1.8\nArchitecture: "+deb_cpu+"\nVersion: 20261102.0\n")
    def line(path, phrase, ref=PRODUCT):
        matches = [l.strip()[2:] for l in git("show",ref+":"+path).splitlines()
                   if l.strip().startswith("- ") and phrase in l]
        assert len(matches) == 1, (path,phrase,matches)
        return matches[0].replace("$$","$")
    cmd = line(".buildkite/pipeline.yaml","make DOCKER_BUILD=false repo.key")
    run("bash","-euc",cmd)
    assert (TREE/"repo.key").stat().st_size > 0
    DOC["presubmit_key_generation"]={"docker_calls":len(trace("docker")),"bazel_calls":len(trace("bazel"))}
    assert not any(DOC["presubmit_key_generation"].values())
    pub = run("gpg","--batch","--export").stdout
    run("gpg","--homedir",VERIFY,"--batch","--import",content=pub)
    fp = [l.split(":")[9] for l in run("gpg","--homedir",VERIFY,"--with-colons","--list-keys").stdout.decode().splitlines() if l.startswith("fpr:")][0]
    assert not any(l.startswith(b"sec:") for l in run("gpg","--homedir",VERIFY,"--with-colons","--list-secret-keys").stdout.splitlines())
    DOC.update(public_fingerprint=fp, bash=run("bash","--version").stdout.decode().splitlines()[0],
               git_source=git("rev-parse","HEAD"), namespace_private=os.readlink("/proc/self/ns/net")!=sys.argv[2],
               network_interfaces=sorted(os.listdir("/sys/class/net")), uid=os.getuid(),
               staged_hashes={str(p.relative_to(STAGE)):hash_file(p) for p in STAGE.glob("*/*")})
    assert DOC["namespace_private"] and DOC["network_interfaces"]==["lo"] and DOC["uid"]!=0
    save()
    return (line(".buildkite/release.yaml","make release RELEASE_NIGHTLY=",BASE),
            line(".buildkite/release.yaml","NIGHTLY=$$RELEASE_NIGHTLY tools/make_release.sh"),
            line(".buildkite/pipeline.yaml","tools/make_release.sh repo.key repo"))


def trace(name):
    path=T/("trace-"+name)
    return [x.decode().split("\0") for x in path.read_bytes().split(b"\0\0") if x] if path.exists() else []


def verify_suite(suite):
    d=DEST/"dists"/suite
    signature=run("gpg","--homedir",VERIFY,"--batch","--status-fd","1","--verify",d/"Release.gpg",d/"Release").stdout
    assert ("VALIDSIG "+DOC["public_fingerprint"]).encode() in signature
    assert run("gpg","--homedir",VERIFY,"--decrypt",d/"InRelease").stdout==(d/"Release").read_bytes()
    release=(d/"Release").read_text()
    headers=[l for l in release.splitlines() if l.startswith(("Origin: ","Components: ","Suite: ","Architectures: "))]
    assert set(headers)=={"Architectures: amd64 arm64","Suite: "+suite,"Components: main","Origin: Google"}
    hashes=release.split("\nSHA256:\n",1)[1].split("\nSHA",1)[0].strip().splitlines()
    indexes={}
    for line in hashes:
        sha,size,rel=line.split()
        f=d/rel
        assert f.stat().st_size==int(size) and hash_file(f)==sha
        indexes[rel]=sha
    assert set(indexes)=={f"main/binary-{a}/Packages{suffix}" for a in CPUS.values() for suffix in ("",".gz",".xz")}
    packages={}
    for raw,cpu in CPUS.items():
        idx=d/"main"/("binary-"+cpu)/"Packages"
        data=idx.read_bytes()
        assert gzip.decompress(idx.with_suffix(".gz").read_bytes())==data==lzma.decompress(idx.with_suffix(".xz").read_bytes())
        fields=dict(l.split(": ",1) for l in data.decode().splitlines() if l and not l.startswith(" ") and ": " in l)
        f=DEST/fields["Filename"]
        assert fields["Package"]=="runsc" and fields["Architecture"]==cpu and fields["Version"]=="20261102.0"
        assert f.stat().st_size==int(fields["Size"]) and hash_file(f)==fields["SHA256"]
        status=run("dpkg-sig","-g","--homedir "+str(VERIFY),"--verify",f).stdout.decode()
        assert "GOODSIG" in status and "_gpgbuilder" in status
        for member in ("debian-binary","control.tar.gz","data.tar.gz"):
            assert run("ar","p",f,member).stdout==run("ar","p",STAGE/raw/"runsc.deb",member).stdout
        changes=f.with_suffix(".changes")
        assert changes.read_bytes()==(STAGE/raw/"runsc.changes").read_bytes()
        normalized=b"\n".join(line for line in data.splitlines() if not line.startswith((b"Size: ",b"MD5sum: ",b"SHA1: ",b"SHA256: ",b"SHA512: ")))
        packages[cpu]={"signed_sha256":fields["SHA256"],"version":fields["Version"],"original_payload":True,
                       "package_metadata_sha256":hashlib.sha256(normalized).hexdigest(),"changes_sha256":hash_file(changes)}
    return {"headers":sorted(headers),"verified_indexes":sorted(indexes),"packages":packages,"signer":DOC["public_fingerprint"]}


def raw_files(relative, expect_python=False, extra=None):
    folder=DEST/relative
    verified={}
    expected={f"{a}/gvisor.tar.{s}": STAGE/a/("gvisor.tar."+s) for a in CPUS for s in SUFFIXES}
    if expect_python:
        expected.update({"python/"+p.name:p for p in (STAGE/"python").iterdir()})
    if extra:
        expected["x86_64/"+extra.name]=extra
    actual={str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()}
    assert actual==set(expected)|{n+".sha512" for n in expected}, (relative,actual,set(expected))
    for name,source in expected.items():
        target=folder/name
        assert hash_file(target)==hash_file(source)
        run("sha512sum","--strict","--check","--",target.name+".sha512",cwd=target.parent)
        verified[name]={"sha256":hash_file(target),"checksum_file":name+".sha512",
                        "checksum_sha256":hash_file(target.with_name(target.name+".sha512"))}
    return verified


def arm(name, command, nightly, suites, raw, extra=None, allow_baseline_failure=False):
    if DEST.exists():
        shutil.rmtree(DEST)
    for p in T.glob("trace-*"):
        p.unlink()
    os.environ["RELEASE_NIGHTLY"]="true" if nightly else "false"
    p=run("bash","-euc",command,check=False)
    docker=trace("docker")
    external={n:trace(n) for n in ("gcloud","pip","pip3","bazel","twine")}
    row=DOC["arms"][name]={"exit":p.returncode,"docker_calls":dict(collections.Counter(a[0] for a in docker)),
        "bazel_calls":len(external["bazel"]),"cloud_calls":len(external["gcloud"]),"twine_calls":len(external["twine"])}
    log=p.stderr+p.stdout
    (T/(name+".log")).write_bytes(log)
    if p.returncode:
        row["diagnostics"]=[line[:280].replace(str(T),"<fixture>") for line in log.decode(errors="replace").splitlines() if "Error " in line or "failed" in line.lower() or "missing" in line.lower()][-8:]
    save()
    assert not any(external[n] for n in ("gcloud","pip","pip3","bazel"))
    if name=="default_make":
        assert docker
    else:
        assert not docker
    if allow_baseline_failure and p.returncode:
        return
    assert p.returncode==0, name
    row["suites"]={s:verify_suite(s) for s in suites}
    row["raw"]={str(d):raw_files(d,py,extra) for d,py in raw}
    if name=="tagged":
        args,=external["twine"]
        assert args[:3]==["upload","--repository-url","https://us-python.pkg.dev/oss-exit-gate-prod/gvisor--pypi"]
        assert sorted(Path(x).name for x in args[3:])==sorted(p.name for p in (STAGE/"python").iterdir())
        row["offline_upload_inputs"]=[Path(x).name for x in args[3:]]
    else:
        assert not external["twine"]
    save()


def negatives():
    missing=T/"missing staged directory"
    empty=T/"empty stage"
    empty.mkdir()
    (empty/"broken-link").symlink_to(missing)
    denied=T/"mixed readability"
    denied.mkdir()
    (denied/"some-input").write_bytes(b"temporary test file")
    (denied/"unreadable").mkdir(mode=0)
    for name, key, stage, expected in (
        ("missing_key",T/"missing-key",STAGE,"Missing release key:"),
        ("missing_stage",TREE/"repo.key",missing,"Missing artifact directory:"),
        ("empty_stage",TREE/"repo.key",empty,"No release artifacts in"),
        ("find_error",TREE/"repo.key",denied,"Permission denied")):
        out=T/("invalid-"+name)
        p=run("tools/make_release.sh",key,out,stage,check=False)
        assert p.returncode and expected.encode() in p.stderr and not out.exists()
        DOC.setdefault("rejected",{})[name]={"exit":p.returncode,"matching_diagnostic":expected,"created_output":out.exists()}
        save()
    (denied/"unreadable").chmod(0o700)


def study():
    old,new,presubmit=prepare()
    git("checkout","-q","--detach",BASE)
    data=[("master/latest",False)]
    arm("default_make",old,False,["master"],data,allow_baseline_failure=True)
    arm("host_make",old.replace("make release","make DOCKER_BUILD=false release"),False,["master"],data)
    git("checkout","-q","--detach",PRODUCT)
    assert git("rev-parse","HEAD")==PRODUCT
    arm("direct",new,False,["master"],data)
    old_result=DOC["arms"]["host_make"]; new_result=DOC["arms"]["direct"]
    assert new_result["raw"]==old_result["raw"]
    a,b=old_result["suites"]["master"],new_result["suites"]["master"]
    assert a["headers"]==b["headers"] and a["verified_indexes"]==b["verified_indexes"]
    assert all(a["packages"][cpu]["package_metadata_sha256"]==b["packages"][cpu]["package_metadata_sha256"] for cpu in CPUS.values())
    extra=STAGE/"sub dir,\nnested"/"nested"/"-probe file\n"
    extra.parent.mkdir(parents=True)
    shutil.copyfile("/usr/bin/true",extra)
    (STAGE/"ignored-link").symlink_to(extra)
    # Use the actual presubmit entry point for the whitespace/recursive case.
    arm("presubmit",presubmit,False,["master"],data,extra)
    extra.unlink()
    negatives()
    arm("nightly",new,True,["nightly"],[("nightly/latest",False)])
    date_dirs=[p for p in (DEST/"nightly").iterdir() if p.name!="latest"]
    dated,=date_dirs
    DOC["arms"]["nightly"]["raw"][str(dated.relative_to(DEST))]=raw_files(dated.relative_to(DEST))
    python=STAGE/"python"
    python.mkdir()
    with zipfile.ZipFile(python/"gvisor-2026.11.02.0-py3-none-any.whl","w") as f:
        f.writestr("gvisor-2026.11.02.0.dist-info/METADATA","Name: gvisor\nVersion: 2026.11.02.0\n")
    with tarfile.open(python/"gvisor-2026.11.02.0.tar.gz","w:gz") as f:
        data=b"gvisor fixture\n"
        h=tarfile.TarInfo("gvisor-2026.11.02.0/README"); h.size=len(data)
        f.addfile(h,io.BytesIO(data))
    git("tag","release-20261102.0")
    arm("tagged",new,False,["release","20261102"],[("release/20261102.0",True),("release/20261102",True),("release/latest",False)])
    DOC["verified"]=True
    save()


if __name__=="__main__":
    if len(sys.argv)>1 and sys.argv[1]=="--isolated":
        try:
            study()
        except Exception as ex:
            DOC["error"]={"type":type(ex).__name__,"message":str(ex)[:400]}
            save()
            raise
        finally:
            for key_home in (T/"generate",VERIFY):
                if key_home.is_dir():
                    run("gpgconf","--homedir",key_home,"--kill","all",cwd=T,check=False)
                    shutil.rmtree(key_home)
            (TREE/"repo.key").unlink(missing_ok=True)
    else:
        outer=os.readlink("/proc/self/ns/net")
        cmd=["sudo","-n","/usr/bin/unshare","--net","--","/usr/bin/setpriv","--reuid="+str(os.getuid()),
             "--regid="+str(os.getgid()),"--clear-groups","--inh-caps=-all","--ambient-caps=-all","--bounding-set=-all","--no-new-privs","/usr/bin/env","-i","PATH=/usr/bin:/bin",
             "LC_ALL=C","HOME="+os.environ["HOME"],"USER="+os.environ["USER"],
             "PUBLICATION_FIXTURE="+str(W),"TEST_TMPDIR="+str(T),
             "/usr/bin/python3",str(Path(__file__).resolve()),"--isolated",outer]
        subprocess.run(cmd, check=True)
