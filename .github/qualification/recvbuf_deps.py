"""Recover public release assets actually selected by these PacketImpact targets."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse

TEMP = Path(os.environ["RUNNER_TEMP"])
OUT = TEMP / "recvbuf"
DIST = OUT / "dist"
REGISTRY = "07dee6b7b923053e558ef6cceb41f280df101fc8"
KNOWN = (
    ("protobuf", "35.1", "75b7b0b36b630c7b6e3aeb07b2e58993fb9494fbb2b08bba0891aaa231d4e4d1", 445066255),
    ("bazel_skylib", "1.9.2", "37cdfbc6faefea94f7b37760a305c98c08981116c2bc9e821e3b423221fad8c8", 480693316),
    ("rules_android", "0.7.1", "7c45b6aaa837fb6f2f23ad11387638cb00fa9f839a04ec564caac70a543a9cd5", 341081786),
)
URL = re.compile(r"https://github[.]com/([\w.-]+)/([\w.-]+)/releases/download/([\w.+-]+)/([\w.+-]+)")
DIR = re.compile(r"/external/([A-Za-z0-9_.-]+)[+]/")
LABELS = ["//test/packetimpact/tests:tcp_rcv_buf_space_" + name + "_test" for name in ("native", "netstack")]


def fetch(url, limit, *, asset=False):
    args = ["curl", "-fsSL", "--max-time", "80", "--retry", "1", "--max-filesize", str(limit)]
    if asset:
        args += ["-H", "Accept: application/octet-stream"]
    cmd = subprocess.run([*args, url], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return cmd.stdout if cmd.returncode == 0 and len(cmd.stdout) <= limit else b""


def acquire(module, version, *, wanted_url=None, pinned=None, asset_id=None):
    for component in (module, version):
        assert re.fullmatch(r"[A-Za-z0-9_.+-]+", component) and component not in (".", "..")
    src = json.loads(fetch(f"https://raw.githubusercontent.com/bazelbuild/bazel-central-registry/{REGISTRY}/modules/{module}/{version}/source.json", 256 << 10))
    url = src["url"]
    match = URL.fullmatch(url)
    assert match and (not wanted_url or url == wanted_url), (module, version, url)
    scheme, encoded = src["integrity"].split("-",1)
    digest = base64.b64decode(encoded, validate=True).hex()
    assert scheme == "sha256" and len(digest) == 64 and (not pinned or digest == pinned)
    owner, repo, tag, name = match.groups()
    assert name not in (".","..") and len(name) < 160
    output = DIST / name
    if output.exists():
        assert hashlib.sha256(output.read_bytes()).hexdigest() == digest
        return None
    blob = fetch("https://mirror.bazel.build/" + url.removeprefix("https://"), 100 << 20)
    via = "bazel-mirror"
    if not blob:
        if asset_id is None:
            rel = json.loads(fetch(f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}", 8 << 20))
            (item,) = [v for v in rel["assets"] if v["name"] == name and v["browser_download_url"] == url]
            asset_id = int(item["id"])
            assert 0 < item["size"] <= 100 << 20
        blob = fetch(f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_id}",
                     100 << 20, asset=True)
        via = "public-github-asset"
    actual = hashlib.sha256(blob).hexdigest()
    assert blob and actual == digest, (module, version, name, actual)
    output.write_bytes(blob)
    return dict(module=module, version=version, registry=REGISTRY, source=url, via=via,
                asset_id=asset_id, bytes=len(blob), sha256=actual)


def main():
    report={"verified":False,"assets":[],"attempts":[],"registry":REGISTRY}
    def save():
        (OUT / "dependencies.json").write_text(json.dumps(report,indent=2)+"\n")
    try:
        DIST.mkdir()
        for module,version,digest,asset in KNOWN:
            report["stage"]=[module,version]
            report["assets"].append(acquire(module,version,pinned=digest,asset_id=asset))
            save()
        args=[str(TEMP/"bazel"),"--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx4g",
              "build","--config=x86_64","--jobs=3","--keep_going","--remote_executor=","--remote_cache=",
              "--bes_backend=","--distdir="+str(DIST),*LABELS]
        for attempt in range(5):
            report["stage"]=["prepare",attempt+1]
            log=OUT/"prepare.log"
            with log.open("wb") as stream:
                rc=subprocess.run(args,stdout=stream,stderr=subprocess.STDOUT).returncode
            text=log.read_text(errors="replace")
            row={"exit":rc,"log_sha256":hashlib.sha256(log.read_bytes()).hexdigest()}
            report["attempts"].append(row)
            save()
            if rc==0:
                report["verified"]=True
                break
            seen=set()
            for line in text.splitlines():
                if "Error downloading" not in line: continue
                modules=set(DIR.findall(line))
                matches=list(URL.finditer(line))
                urls={m.group(0) for m in matches}
                if len(modules)==len(urls)==1:
                    seen.add((next(iter(modules)),next(iter(urls))))
            row["missing"] = sorted(seen)
            row["errors"]=[line[:700] for line in text.splitlines() if line.startswith("ERROR:")][:6]
            progress=False
            for module,url in sorted(seen):
                # A tag-derived version is only a lookup hint; the pinned BCR record
                # must name the byte-for-byte identical URL and content digest.
                match=URL.fullmatch(url)
                version=match.group(3).removeprefix("v")
                if attempt==4 or len(report["assets"])>=10: break
                report["stage"]=["recover",module,version,url]
                info=acquire(module,version,wanted_url=url)
                if info:
                    report["assets"].append(info)
                    progress=True
                save()
            if not progress: break
        assert report["verified"], "targets did not finish building; inspect the original errors"
    except Exception as e:
        report["error"]=type(e).__name__+" "+str(e)[:600]
        raise
    finally:
        save()


if __name__=="__main__":
    main()
