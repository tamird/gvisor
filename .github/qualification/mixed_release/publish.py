"""Run the actual publication command offline on the downloaded provider inputs."""

import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import traceback

sys.path.insert(0,str(Path(__file__).resolve().parent))
from mixed import D, MANIFEST, PY, SOURCE, STAMP, VERSION, blocks, hashfile, read, save


def ck(args,**kw):
    return subprocess.check_output(args,stderr=subprocess.PIPE,**kw)


def verify_suite(repo,suite,verify,r):
    dist=repo/"dists"/suite
    signature=ck(["gpg","--homedir",verify,"--batch","--status-fd","1","--verify",dist/"Release.gpg",dist/"Release"])
    assert ("VALIDSIG "+r["public_fingerprint"]).encode() in signature
    assert ck(["gpg","--homedir",verify,"--decrypt",dist/"InRelease"])==(dist/"Release").read_bytes()
    rel=(dist/"Release").read_text()
    assert set(s for s in rel.splitlines() if s.startswith(("Suite: ","Components: ","Origin: ","Architectures: ")))=={
        "Suite: "+suite,"Components: main","Origin: Google","Architectures: amd64 arm64"}
    indexes={}
    for line in rel.split("\nSHA256:\n")[1].split("\nSHA",1)[0].strip().splitlines():
        digest,size,name=line.split();p=dist/name
        assert hashfile(p)==digest and p.stat().st_size==int(size)
        indexes[name]=digest
    assert set(indexes)=={f"main/binary-{c}/Packages{s}" for c in ("amd64","arm64") for s in ("",".gz",".xz")}
    results={"indexes":indexes,"packages":{}}
    for cpu in ("amd64","arm64"):
        ix=dist/"main"/("binary-"+cpu)/"Packages";data=ix.read_bytes()
        assert gzip.decompress(ix.with_suffix(".gz").read_bytes())==data==lzma.decompress(ix.with_suffix(".xz").read_bytes())
        fields=dict(l.split(": ",1) for l in data.decode().splitlines() if l and not l.startswith(" ") and ": " in l)
        deb=repo/fields["Filename"]
        assert fields["Architecture"]==cpu and fields["Version"]==STAMP.removeprefix("release-") and fields["Package"]=="runsc"
        assert fields["SHA256"]==hashfile(deb) and int(fields["Size"])==deb.stat().st_size
        status=ck(["dpkg-sig","--verify",deb],env={**os.environ,"GNUPGHOME":str(verify)})
        assert b"GOODSIG _gpgbuilder" in status
        source=Path(next(x["root"] for x in read("outputs")["fresh"] if x["arch"]==cpu and x["owner"]=="//debian:debian"))
        for member in ("debian-binary","control.tar.gz","data.tar.gz"):
            assert ck(["ar","p",source,member])==ck(["ar","p",deb,member])
        results["packages"][cpu]={"signed_sha256":fields["SHA256"],"unsigned_sha256":hashfile(source),"matched_original_members":True}
    return results


def sign():
    assert not (D/"client.rc").exists()
    src=Path.cwd();out=read("outputs");assert out["verified"] and out["source"]==SOURCE
    root=D/"consumer";root.mkdir();(root/"tools").mkdir()
    # Preserve the actual working-directory Bazel links without another payload copy.
    for name in ("bazel-out","bazel-bin"):
        (root/name).symlink_to(src/name)
    for name in ("make_apt.sh","make_python_release.sh"):
        (root/"tools"/name).symlink_to(src/"tools"/name)
    cmd=blocks()[1]
    marker=root/"tools"/"make_release.sh"
    marker.write_text("#!/usr/bin/python3\nimport os,sys,pathlib\n"+
                      "pathlib.Path('observed.argv0').write_bytes(b'\\0'.join(map(os.fsencode,sys.argv[1:]))+b'\\0')\n"+
                      "p="+repr(str(src/"tools/make_release.sh"))+"\nos.execv(p,[p,*sys.argv[1:]])\n")
    marker.chmod(0o755)
    os.chdir(root)
    r={"source":SOURCE,"verified":False,"private_netns":os.readlink("/proc/self/ns/net")!=sys.argv[2],
       "links":sorted(n for _,n in socket.if_nameindex()),"uid":os.getuid()}
    st=dict(l.split(":",1) for l in Path("/proc/self/status").read_text().splitlines() if ":" in l)
    r.update(caps=int(st["CapEff"],16),no_new_privs=int(st["NoNewPrivs"]))
    assert r["private_netns"] and r["links"]==["lo"] and r["uid"]!=0 and r["caps"]==0 and r["no_new_privs"]==1
    assert MANIFEST.read_bytes()==(src/MANIFEST).read_bytes()
    save("publication",r)
    ck(["git","init","-q"])
    ck(["git","-c","user.name=Test","-c","user.email=test@example.invalid","-c","commit.gpgsign=false","commit","-q","--allow-empty","-m","Synthetic release tag"])
    ck(["git","tag",STAMP]);ck(["git","config","url."+(root/".git").as_uri()+".insteadOf","https://github.com/google/gvisor.git"])
    bins=root/"bin";bins.mkdir()
    for name in ("twine","gcloud","pip","pip3","docker","bazel"):
        p=bins/name
        p.write_text("#!/bin/bash\n"+r'''printf '%s\0' "$@" >> "$PUBLISH_TRACE_DIR/called-@NAME@"
[[ @NAME@ == twine && "$1" == upload ]] || exit 125
'''.replace("@NAME@",name))
        p.chmod(0o755)
    os.environ["PATH"]=str(bins)+":/usr/bin:/bin"
    assert shutil.which("twine",path=os.environ["HOME"]+"/.local/bin:/usr/local/bin:"+os.environ["PATH"])==str(bins/"twine")
    os.environ["RELEASE_NIGHTLY"]="false"
    os.environ["PUBLISH_TRACE_DIR"]=str(root)
    generate=root/"generate";verify=root/"verify"
    generate.mkdir(mode=0o700);verify.mkdir(mode=0o700)
    os.environ["GNUPGHOME"]=str(generate)
    try:
        ck(["gpg","--batch","--pinentry-mode","loopback","--passphrase","","--quick-generate-key","Offline test <test@example.invalid>","rsa2048","sign","1d"])
        (root/"repo.key").write_bytes(ck(["gpg","--batch","--pinentry-mode","loopback","--passphrase","","--export-secret-keys"]))
        public=ck(["gpg","--export"]);ck(["gpg","--homedir",verify,"--batch","--import"],input=public)
        r["public_fingerprint"]=[l.split(":")[9] for l in ck(["gpg","--homedir",verify,"--with-colons","--list-keys"]).decode().splitlines() if l.startswith("fpr:")][0]
        proc=subprocess.run(["bash","-euc",cmd],capture_output=True)
        r["exit"]=proc.returncode
        if proc.returncode:
            r["diagnostic"]=[x[:270].replace(str(D),"<fixture>") for x in proc.stderr.decode(errors="replace").splitlines()[-8:]]
        save("publication",r);assert proc.returncode==0
        args=(root/"observed.argv0").read_bytes()
        assert args==b"repo.key\0repo\0"+MANIFEST.read_bytes()
        r.update(argv_matches_manifest=True,argv_sha256=hashlib.sha256(args).hexdigest())
        assert not any((root/("called-"+n)).exists() for n in ("gcloud","pip","pip3","docker","bazel"))
        repo=root/"repo"; name=STAMP.removeprefix("release-");date=name.split(".")[0]
        r["suites"]={suite:verify_suite(repo,suite,verify,r) for suite in ("release",date)}
        sources={}
        for a in out["fresh"]:
            if a["owner"]=="//debian:debian": continue
            directory="python" if a["owner"]==PY else {"amd64":"x86_64","arm64":"aarch64"}[a["arch"]]
            for path,stat in a["files"].items():
                sources[directory+"/"+Path(path).name]=stat["sha256"]
        r["raw"]={}
        for tier in (name,date,"latest"):
            expected={n:h for n,h in sources.items() if tier!="latest" or not n.startswith("python/")}
            directory=repo/"release"/tier
            assert {str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file()}==set(expected)|{n+".sha512" for n in expected}
            for n,h in expected.items():
                p=directory/n;assert hashfile(p)==h;ck(["sha512sum","--check","--strict","--",p.name+".sha512"],cwd=p.parent)
            r["raw"][tier]=expected
        twine=(root/"called-twine").read_bytes().split(b"\0")[:-1]
        assert twine[:3]==[b"upload",b"--repository-url",b"https://us-python.pkg.dev/oss-exit-gate-prod/gvisor--pypi"]
        assert {Path(os.fsdecode(f)).name for f in twine[3:]}=={Path(n).name for n in out["python_files"]}
        r.update(twine_offline=True,verified=True);save("publication",r)
    finally:
        for home in (generate,verify):
            subprocess.run(["gpgconf","--homedir",home,"--kill","all"],capture_output=True)
            shutil.rmtree(home,ignore_errors=True)
        (root/"repo.key").unlink(missing_ok=True)


if __name__=="__main__":
    try:
        if len(sys.argv)>1 and sys.argv[1]=="offline": sign()
        else:
            assert not (D/"client.rc").exists()
            subprocess.run(["sudo","-n","/usr/bin/unshare","--net","--","/usr/bin/setpriv","--reuid="+str(os.getuid()),"--regid="+str(os.getgid()),
                "--clear-groups","--inh-caps=-all","--ambient-caps=-all","--bounding-set=-all","--no-new-privs",
                "/usr/bin/env","-i","PATH=/usr/bin:/bin","HOME="+os.environ["HOME"],"RUNNER_TEMP="+os.environ["RUNNER_TEMP"],
                "LC_ALL=C","/usr/bin/python3","-I",str(Path(__file__).resolve()),"offline",os.readlink("/proc/self/ns/net")],check=True)
    except Exception as ex:
        f=traceback.extract_tb(ex.__traceback__)[-1]
        save("error-publication",{"type":type(ex).__name__,"line":f.lineno,"expression":f.line[:200]})
        raise
