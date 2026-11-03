"""Qualify the public Bazel and Make website entry points against the retained site."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
from urllib.request import urlopen
import xml.etree.ElementTree as ET

result = Path(os.environ["RUNNER_TEMP"]) / "declared-website-sdk"
result.mkdir(exist_ok=True)
def record(name, datum):
    (result / f"{name}.json").write_text(json.dumps(datum, indent=2) + "\n")
def digest(data):
    return hashlib.sha256(data).hexdigest()
def site_files(tarball):
    with tarfile.open(tarball) as t:
        return {m.name.removeprefix("./"): (m.mode, t.extractfile(m).read()) for m in t if m.isfile() or m.islnk()}
def archive_members(blob):
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        return {m.name: dict(mode=oct(m.mode), type=m.type.decode(), uid=m.uid, gid=m.gid,
                             mtime=m.mtime, payload=digest(tar.extractfile(m).read()) if m.isfile() else None) for m in tar}
def xml_without_order(blob):
    urls=re.findall(rb"<url>.*?</url>", blob, re.DOTALL)
    assert len(urls)==len(ET.fromstring(blob))>0
    return re.sub(rb"<url>.*?</url>",b"",blob,flags=re.DOTALL),Counter(urls)
secret=os.environ.pop("BUILDBUDDY_API_KEY")
assert secret and "\n" not in secret
rc=Path(os.environ["RUNNER_TEMP"])/"website-product.bazelrc"
rc.write_text("\n".join("build "+s for s in [
    "--jobs=4", "--noshow_progress", "--color=no", "--verbose_failures",
    "--bes_backend=grpcs://remote.buildbuddy.io", "--bes_results_url=https://app.buildbuddy.io/invocation/",
    "--bes_header=x-buildbuddy-api-key="+secret,
])+"\n")
rc.chmod(0o600)
def run(tag,argv, env=None, required=True):
    record("start-"+tag,{"phase":phase,"targets":[v for v in argv if v.startswith("//website:") or v in ("//tools/licensecheck/main:licensecheck","website-build")]})
    runenv=os.environ|dict(env or {})
    cp=subprocess.run(argv, env=runenv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=4000)
    log=cp.stdout.decode(errors="replace").replace(secret,"[redacted]")
    for token in (os.environ.get("GITHUB_TOKEN"),os.environ.get("GH_TOKEN")):
        if token: log=log.replace(token,"[redacted]")
    (result/(tag+".log")).write_text(log)
    print(tag, cp.returncode, "\n".join(line[:550] for line in log.splitlines() if any(k in line for k in ("ERROR:", "Error", "Build completed", "invocation/", "no license entry","missing ", "license not allowed")))[:3500],flush=True)
    record("exit-"+tag,{"returncode":cp.returncode,"invocation":invocation(log),"errors":[line[:400] for line in log.splitlines() if line.startswith("ERROR:")][:5] if cp.returncode else []})
    if required and cp.returncode: raise RuntimeError(f"{tag} exited {cp.returncode}")
    return cp.returncode,log
def invocation(log):
    refs=re.findall(r"invocation/([-0-9a-f]{36})",log)
    return refs[0] if refs else None
bazel=["bazel","--bazelrc="+str(rc)]
original="--//website:publication_time=2026-10-30T12:00:00Z"
phase=os.environ.get("QUALIFY_PHASE","site")
try:
    if phase=="audit":
        catalog=result/"catalog.yaml"
        original_catalog=Path("tools/licensecheck/dependencies.yaml").read_bytes()
        catalog.write_bytes(original_catalog)
        argv=bazel+["run","//tools/licensecheck/main:licensecheck","--","--yaml="+str(catalog)]
        env={"MAKEFLAGS":"DOCKER_BUILD=false","DOCKER_HOST":"unix:///no-docker-daemon"}
        _,fetch=run("fetch",argv+["--mode=fetch"],env=env)
        _,verify=run("verify",argv+["--mode=verify"],env=env)
        generated=catalog.read_bytes()
        assert b"/v0.3.24/rules_img-v0.3.24.tar.gz" in generated
        record("audit",dict(entries=generated.count(b"- dependency:"),catalogSHA256=digest(generated),
                            fetch=invocation(fetch),verify=invocation(verify)))
    elif phase=="site":
        _,log=run("normal-build",bazel+["build",original,"//website:site_image","//website:files"],env={"DOCKER_HOST":"unix:///no-docker-daemon"})
        _,listing=run("rootfs",bazel+["cquery",original,"--output=files","//website:files"])
        outputs=[Path(l) for l in listing.splitlines() if l.startswith("bazel-out/") and Path(l).is_file()]
        assert len(outputs)==1, outputs
        import shutil
        shutil.copyfile(outputs[0], result/"verified-layer.tgz")
        with tarfile.open(outputs[0]) as archive:
            item=next(m for m in archive if m.name.removeprefix("./")=="_site/docs/logo.png")
            record("layer-logo", dict(name=item.name, hardlink=item.islnk(), target=item.linkname,
                   sha256=digest(archive.extractfile(item).read())))
            assert item.islnk(), "expected the duplicate source logo to use the tar writer's real hardlink"
        baseline=site_files(Path(os.environ["RUNNER_TEMP"])/"stock-site/stock-site.tgz")
        current=site_files(outputs[0])
        assert baseline.keys()==current.keys(),(baseline.keys()-current.keys(),current.keys()-baseline.keys())
        paths=sorted(k for k in baseline if k.startswith("_site/"))
        changed=[k for k in paths if baseline[k][1]!=current[k][1]]
        # Server and trust store are verified as serving-image inputs independently
        # of generated site content. Current master has new runtime commits.
        report=dict(duplicate_feature_warnings=log.count("warning: already initialized constant"), invocation=invocation(log), public=len(paths), raw_identical=len(paths)-len(changed), changed=changed,
            root_files=len(current), server_mode=oct(current['server'][0]), ca_sha256=digest(current['etc/ssl/cert.pem'][1]))
        record("normal-build",report)
        assert not set(changed)-{'_site/sitemap.xml','_site/css.tar'},changed
        assert report['server_mode']=='0o755' and report['ca_sha256']==digest(baseline['etc/ssl/cert.pem'][1])
        a,b=baseline['_site/sitemap.xml'][1],current['_site/sitemap.xml'][1]
        assert xml_without_order(a)==xml_without_order(b)
        report['unchanged_sitemap_records']=len(ET.fromstring(b))
        report['sitemap_sha256']=digest(b)
        old_members=archive_members(baseline['_site/css.tar'][1]); new_members=archive_members(current['_site/css.tar'][1])
        assert old_members.keys()==new_members.keys()
        assert all(tuple(old_members[k][f] for f in ('mode','type','payload'))==tuple(new_members[k][f] for f in ('mode','type','payload')) for k in old_members)
        assert all(m['uid']==m['gid']==m['mtime']==0 for m in new_members.values())
        report['canonical_css_sha256']=digest(current['_site/css.tar'][1]);record("normal-build",report)
        _,log=run("preview",bazel+["build","//website:site"],env={"DOCKER_HOST":"unix:///no-docker-daemon"})
        stamp=int(next(s.split()[1] for s in Path("bazel-out/volatile-status.txt").read_text().splitlines() if s.startswith("BUILD_TIMESTAMP ")))
        feed=ET.parse("bazel-bin/website/site/blog/index.xml").getroot().find("{http://www.w3.org/2005/Atom}updated").text
        actual=int(datetime.fromisoformat(feed).timestamp())
        record("preview",{"status_time":stamp,"feed_time":actual,"feed":feed,"invocation":invocation(log)})
        assert actual==stamp,(actual,stamp)
    elif phase=="serve":
        tag="gvisor-website:declared-product"
        _,log=run("normal-make",["make","website-build","BAZEL=bazel --bazelrc="+str(rc),
                                  "WEBSITE_IMAGE="+tag,"WEBSITE_PUBLICATION_TIME=2026-10-30T12:00:00Z"])
        _,cid=run("start",["docker","run","--detach","--rm","-p","127.0.0.1::8080",tag,"--custom-domain=*"])
        cid=cid.strip()
        try:
            _,port=run("port",["docker","port",cid,"8080/tcp"])
            host=port.strip().splitlines()[0]
            baseline=site_files(Path(os.environ["RUNNER_TEMP"])/"stock-site/stock-site.tgz")
            proofs={}
            for path in ('/','/blog/index.xml','/css/main.css','/archive.key','/docs/logo.png'):
                url=f"http://{host}{path}"
                for attempt in range(20):
                    try:
                        with urlopen(url,timeout=5) as response: content=response.read()
                    except OSError:
                        if attempt==19:raise
                        time.sleep(.25)
                    else:break
                key="_site/"+("index.html" if path=="/" else path[1:])
                assert content==baseline[key][1],path
                proofs[path]=digest(content)
            record("served",dict(routes=proofs,tag=tag,invocation=invocation(log)))
        finally:
            run("stop",["docker","stop",cid],required=False)
    else: raise ValueError(phase)
finally:
    rc.unlink(missing_ok=True)
