"""Compare the shipping host publishers on the original Buildkite inputs, offline."""
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
from mixed import D, PY, SOURCE, STAGE, STAMP, VERSION, blocks, read, save

BASE="9ee2906f518e1d0f9fd109e9e1feb2812096418b"
CACHE={}
ARCHIVE={}

def digests(p):
    st=p.stat();k=(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
    if k not in CACHE:
        h=hashlib.sha256();w=hashlib.sha512()
        with p.open("rb") as f:
            for b in iter(lambda:f.read(1<<20),b""):h.update(b);w.update(b)
        CACHE[k]=(h.hexdigest(),w.hexdigest())
    return CACHE[k]

def hashfile(p):
    return digests(p)[0]

def member_hash(p,member):
    digest=hashfile(p);k=(digest,member)
    if k not in ARCHIVE:
        with subprocess.Popen(["ar","p",p,member],stdout=subprocess.PIPE,stderr=subprocess.PIPE) as a:
            h=hashlib.sha256()
            for b in iter(lambda:a.stdout.read(1<<20),b""):h.update(b)
            a.stdout.close();assert a.wait()==0
        ARCHIVE[k]=h.hexdigest()
    return ARCHIVE[k]

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
            assert member_hash(source,member)==member_hash(deb,member)
        results["packages"][cpu]={"signed_sha256":fields["SHA256"],"unsigned_sha256":hashfile(source),"matched_original_members":True}
    return results


def snapshot(repo,case,variant,r,verify,sources,changes=False):
    files={p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()} if repo.exists() else set()
    raw={}
    expected={}
    date=STAMP.removeprefix("release-").split(".")[0];point=STAMP.removeprefix("release-")
    if case=="nightly":
        from datetime import date as clock_date
        aliases={"nightly/latest":False,"nightly/"+clock_date.today().isoformat():False}; suites=["nightly"]
    elif case=="head" or case=="prebuilt": aliases={"master/latest":False};suites=["master"]
    elif case=="ignored":aliases={};suites=[]
    else:
        aliases={"release/"+date:True,"release/"+point:True};suites=[date]
        if case!="older":aliases["release/latest"]=False;suites.append("release")
    for prefix,py in aliases.items():
        for n,h in sources.items():
            if n.endswith(".deb") or (n.startswith("python/") and not py):continue
            raw[prefix+"/"+n]=h["sha256"]
    normal=set(raw)|{p+".sha512" for p in raw}
    for n,digest in raw.items():
        path=repo/n
        sha,sha512=digests(path);assert sha==digest
        assert (repo/(n+".sha512")).read_bytes()==(sha512+"  "+path.name+"\n").encode()
    for s in suites:
        d=verify_suite(repo,s,verify,r)
        for cpu,x in d["packages"].items():
            pool="pool/"+point+"/binary-"+cpu+"/runsc.deb"
            normal.add(pool)
            if changes:
                c=repo/pool.replace(".deb",".changes")
                assert c.read_bytes()==("optional "+cpu+"\n").encode();normal.add(str(c.relative_to(repo)))
        normal.update("dists/"+s+"/"+n for n in ("Release","InRelease","Release.gpg",*d["indexes"]))
        expected[s]=d
    assert files==normal,(files-normal,normal-files)
    return {"file_count":len(files),"paths":sorted(files),"raw":raw,"suites":expected}

def compare(stock,fixed):
    assert stock["paths"]==fixed["paths"] and stock["raw"]==fixed["raw"]
    assert stock["twine"]==fixed["twine"]
    for suite,a in stock["suites"].items():
        b=fixed["suites"][suite]
        assert set(a["indexes"])==set(b["indexes"])
        for cpu,ar in a["packages"].items():
            br=b["packages"][cpu]
            assert ar["unsigned_sha256"]==br["unsigned_sha256"] and ar["matched_original_members"] and br["matched_original_members"]
    return {"paths":stock["file_count"],"raw":len(stock["raw"]),"signed_suites":sorted(stock["suites"])}

def sign():
    assert not (D/"client.rc").exists()
    src=Path.cwd();out=read("outputs");assert out["materialized"] and out["source"]==SOURCE and read("build")["exit"]==0
    r={"source":SOURCE,"stock":BASE,"verified":False,"pairs":{},"private_netns":os.readlink("/proc/self/ns/net")!=sys.argv[2],
       "links":sorted(n for _,n in socket.if_nameindex()),"uid":os.getuid()}
    st=dict(l.split(":",1) for l in Path("/proc/self/status").read_text().splitlines() if ":" in l)
    r.update(caps=int(st["CapEff"],16),bounding=int(st["CapBnd"],16),no_new_privs=int(st["NoNewPrivs"]))
    assert r["private_netns"] and r["links"]==["lo"] and r["uid"]!=0 and r["caps"]==r["bounding"]==0 and r["no_new_privs"]==1
    save("publication",r)
    work=D/"consumer";work.mkdir()
    legacy=work/"prebuilt";legacy.mkdir()
    for name in out["stage"]:
        p=legacy/name;p.parent.mkdir(exist_ok=True)
        os.link((src/STAGE/name).resolve(),p)
    bins=work/"bin";bins.mkdir()
    for name in ("twine","gcloud","pip","pip3","docker","bazel","sha512sum"):
        p=bins/name
        p.write_text("#!/bin/bash\nprintf '%s\\0' \"$@\" >> \"$TRACE/"+name+"\"\nprintf '\\0' >> \"$TRACE/"+name+"\"\n"+
            ("exec /usr/bin/sha512sum \"$@\"\n" if name=="sha512sum" else '[[ "$1" == upload ]]\n' if name=="twine" else "exit 125\n"))
        p.chmod(0o755)
    os.environ["PATH"]=str(bins)+":/usr/bin:/bin"
    assert shutil.which("twine",path=os.environ["HOME"]+"/.local/bin:/usr/local/bin:"+os.environ["PATH"])==str(bins/"twine")
    generate=work/"key";verify=work/"verify";generate.mkdir(mode=0o700);verify.mkdir(mode=0o700)
    os.environ["GNUPGHOME"]=str(generate)
    try:
        ck(["gpg","--batch","--pinentry-mode","loopback","--passphrase","","--quick-generate-key","Offline test <test@example.invalid>","rsa2048","sign","1d"])
        key=work/"key.export";key.write_bytes(ck(["gpg","--batch","--pinentry-mode","loopback","--passphrase","","--export-secret-keys"]))
        ck(["gpg","--homedir",verify,"--batch","--import"],input=ck(["gpg","--export"]))
        r["public_fingerprint"]=[l.split(":")[9] for l in ck(["gpg","--homedir",verify,"--with-colons","--list-keys"]).decode().splitlines() if l.startswith("fpr:")][0]
        roots={}
        for arm in ("stock","candidate"):
            tree=work/arm;tree.mkdir()
            (tree/"tools").symlink_to((D/"stock" if arm=="stock" else src)/"tools")
            for n in ("bazel-out","bazel-bin","images",".bazelversion"):(tree/n).symlink_to(src/n)
            (tree/"repo.key").symlink_to(key);(tree/"Makefile").symlink_to(src/"Makefile")
            ck(["git","init","-q"],cwd=tree)
            ck(["git","-c","user.name=Test","-c","user.email=test@example.invalid","-c","commit.gpgsign=false","commit","-q","--allow-empty","-m","Release fixture"],cwd=tree)
            roots[arm]=tree
        remote=work/"remote";ck(["git","clone","-q","--bare",roots["stock"],remote]);commit=ck(["git","--git-dir",remote,"rev-parse","HEAD"]).decode().strip()
        for tree in roots.values():ck(["git","config","url."+remote.as_uri()+".insteadOf","https://github.com/google/gvisor.git"],cwd=tree)
        sources=out["stage"]
        def exercise(arm,case,repeat=False):
            tree=roots[arm];os.chdir(tree);repo=tree/"repo"
            if repo.exists() and not repeat:shutil.rmtree(repo)
            trace=work/("trace-"+arm+"-"+case+("-repeat" if repeat else ""));trace.mkdir();os.environ["TRACE"]=str(trace)
            for t in ck(["git","tag","-l"]).decode().splitlines():ck(["git","tag","-d",t])
            if case in ("tagged","older"):
                ck(["git","tag",STAMP])
            elif case=="ignored":
                ck(["git","tag","buildkite-test-branch"]);ck(["git","tag",STAMP+"-staging"])
            os.environ["NIGHTLY"]=os.environ["RELEASE_NIGHTLY"]="true" if case=="nightly" else "false"
            if case=="prebuilt" and arm=="candidate":
                cmd=["make","DOCKER_BUILD=false","release","RELEASE_ARTIFACTS="+str(legacy),"PYTHON_RELEASE_DEST="+str(legacy/"python")]
            elif arm=="stock":
                cmd=["tools/make_release.sh","repo.key","repo",*(str(legacy/n) for n in sorted(sources))]
            else:cmd=["bash","-euc",blocks_from_source]
            p=subprocess.run(cmd,capture_output=True)
            item={"case":case,"arm":arm,"exit":p.returncode,"stdout_sha256":hashlib.sha256(p.stdout).hexdigest(),"stderr_sha256":hashlib.sha256(p.stderr).hexdigest()}
            name="publication-"+arm+"-"+case+("-repeat" if repeat else "")
            if p.returncode:item["diagnostic"]=[x[:250].replace(str(D),"<fixture>") for x in p.stderr.decode(errors="replace").splitlines()[-5:]]
            save(name,item);assert p.returncode==0
            assert not any((trace/n).exists() for n in ("gcloud","docker","pip","pip3","bazel"))
            twine=[]
            if (trace/"twine").exists():
                twine=(trace/"twine").read_bytes().split(b"\0")[:-2]
                assert twine[:3]==[b"upload",b"--repository-url",b"https://us-python.pkg.dev/oss-exit-gate-prod/gvisor--pypi"]
                twine=[Path(os.fsdecode(x)).name for x in twine[3:]]
                assert set(twine)=={Path(n).name for n in sources if n.startswith("python/")}
            assert bool(twine)==(case in ("tagged","older"))
            item.update(snapshot(repo,case,arm,r,verify,sources,changes=case=="prebuilt"),twine=sorted(twine))
            hashes=(trace/"sha512sum").read_bytes().split(b"\0\0")[:-1] if (trace/"sha512sum").exists() else []
            item["host_sha512_calls"]=len(hashes)
            if arm=="candidate" and case!="prebuilt":assert not hashes
            if arm=="candidate" and case=="prebuilt":assert len(hashes)==6
            save(name,item)
            return item
        os.chdir(src);blocks_from_source=blocks()[1]
        for case in ("head","nightly","tagged","older","ignored","prebuilt"):
            if case=="older":ck(["git","--git-dir",remote,"tag","release-20261108.0",commit])
            if case=="prebuilt":
                for d,c in (("aarch64","arm64"),("x86_64","amd64")):(legacy/d/"runsc.changes").write_text("optional "+c+"\n")
            a=exercise("stock",case)
            if (roots["stock"]/"repo").exists():shutil.rmtree(roots["stock"]/"repo")
            b=exercise("candidate",case)
            r["pairs"][case]=compare(a,b);save("publication",r)
            if case=="tagged":
                target=roots["candidate"]/"repo/release/latest/x86_64/gvisor.tar.bz2"
                target.chmod(0o444)
                again=exercise("candidate",case,repeat=True)
                assert again["paths"]==b["paths"] and again["raw"]==b["raw"]
                for suite,facts in b["suites"].items():
                    assert facts["packages"]==again["suites"][suite]["packages"]
                r["read_only_retry"]=True;save("publication",r)
            if (roots["candidate"]/"repo").exists():shutil.rmtree(roots["candidate"]/"repo")
        # Remove only our own legacy fixture link. Its immutable target remains.
        missing=legacy/"aarch64/runsc.deb";missing.unlink()
        os.chdir(roots["candidate"]);target=work/"incomplete"
        res=subprocess.run(["make","DOCKER_BUILD=false","release","RELEASE_ARTIFACTS="+str(legacy),"PYTHON_RELEASE_DEST="+str(legacy/"python"),"RELEASE_ROOT="+str(target)],capture_output=True)
        assert res.returncode and str(missing).encode() in res.stderr and not target.exists()
        assert all(hashfile((src/STAGE/n).resolve())==v["sha256"] for n,v in sources.items())
        r.update(missing_arm64_rejected=True,source_payloads_unchanged=True,verified=True);save("publication",r)
    finally:
        for home in (generate,verify):subprocess.run(["gpgconf","--homedir",home,"--kill","all"],capture_output=True)
        (work/"key.export").unlink(missing_ok=True)



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
