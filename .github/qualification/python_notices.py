import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import sys

work = Path(os.environ["RUNNER_TEMP"]) / "python-license"
receipt = work / "results.json"
pinned = {
    "x86_64": "2fc493f8f14ff8bf743eb34934bf1edbac518b023e8aa72bc7c0006ee13495e4",
    "aarch64": "976cfd9b50e2fd37c21b74816f4372abc4db21ebaef4bdf3e1690dcb9d1c66c1",
}

def store():
    receipt.write_text(json.dumps(result, indent=2) + "\n")

def examine(arch, digest):
    url = (
        "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/"
        f"cpython-3.11.18%2B20261026-{arch}-unknown-linux-musl-lto%2Bstatic-full.tar.zst"
    )
    source = Path("MODULE.bazel").read_text()
    assert digest in source and url in source
    archive = work / (arch + ".tar.zst")
    subprocess.run(["curl", "-fsSL", "--max-time", "160", "--retry", "2", "--proto", "=https", "--proto-redir", "=https", url, "-o", archive], check=True)
    with archive.open("rb") as f:
        assert hashlib.file_digest(f, "sha256").hexdigest() == digest
    notices, metadata, manifest_licenses = {}, None, []
    with subprocess.Popen(["zstd", "-dc", archive], stdout=subprocess.PIPE) as zstd:
        with tarfile.open(fileobj=zstd.stdout, mode="r|") as tf:
            for ent in tf:
                if not ent.isfile():
                    continue
                name = ent.name.removeprefix("./")
                if name == "python/PYTHON.json":
                    assert ent.size <= 8_000_000
                    buf = tf.extractfile(ent).read()
                    meta = json.loads(buf)
                    manifest_licenses = sorted(k for k in meta if "licens" in k.lower())
                    metadata = hashlib.sha256(buf).hexdigest()
                if name.startswith("python/licenses/") or name == "python/install/lib/python3.11/LICENSE.txt":
                    assert len(notices)<180 and re.fullmatch(r"[A-Za-z0-9._+ /-]+", name) and ent.size < 2_000_000
                    buf=tf.extractfile(ent).read()
                    notices[name]=hashlib.sha256(buf).hexdigest()
        zstd.stdout.read()
        assert zstd.wait() == 0
    result["archives"][arch] = {"archive_sha256":digest,"metadata_sha256":metadata,"metadata_license_keys":manifest_licenses,"notices":notices}
    store()
    assert metadata and notices
    archive.unlink()

def verify():
    git = lambda *args: subprocess.check_output(["git",*args],text=True).strip()
    catalog=Path("tools/licensecheck/dependencies.yaml").read_bytes()
    names=re.findall(rb"(?m)^- dependency: ([^\s]+)$",catalog)
    assert len(names)==len(set(names))
    row = result["verify"]={"source":git("rev-parse","HEAD"),"catalog_entries":len(names),"catalog_sha256":hashlib.sha256(catalog).hexdigest(),"checker_git_blob":git("rev-parse","HEAD:tools/licensecheck/licensecheck.go"),"direct_archive_records":sorted(x.decode() for x in names if x.startswith(b"release_static_cpython"))}
    store()
    output=work/"verify.log"
    with output.open("wb") as out:
        p=subprocess.run(["timeout","-k","15","1080","make","DOCKER_BUILD=false","license-check"],stdout=out,stderr=subprocess.STDOUT)
    buf=output.read_bytes();text=re.sub(r"\x1b\[[0-9;]*[A-Za-z]","",buf.decode(errors="replace"))
    counts=re.findall(r"(?m)^all (\d+) dependencies have up-to-date license entries that conform to governance/licensing\.yaml$",text)
    problems=re.findall(r"(?m)^licensecheck: (\d+) problems;",text)
    row.update(exit=p.returncode,log_sha256=hashlib.sha256(buf).hexdigest(),log_bytes=len(buf),verified_count=int(counts[0]) if len(counts)==1 else None,problem_count=int(problems[0]) if len(problems)==1 else None)
    for key,pattern in {
        "missing":r"(?m)^missing entry for ([\w@./+-]+)$",
        "stale":r"(?m)^stale entry for ([\w@./+-]+), which is no longer a dependency$",
        "unsupported":r"(?m)^.+?unsupported repository rule ([\w@./+-]+)\b",
    }.items():
        row[key]=sorted(set(re.findall(pattern,text)))[:100]
    row["fingerprints"]=[s for s in ("could not resolve","no matching toolchain","no such package","no such target","error downloading","permission denied","exec format error","checking bazel's dependencies","could not determine archives","unknown repository rule","failed to fetch the repository","no space left on device") if s in text.lower()]
    if p.returncode:
        print("Stock catalog verification exit",p.returncode,flush=True)
        for line in text.splitlines():
            if (line.startswith("ERROR:") or line.startswith("licensecheck:")) and len(line)<2500:
                print(line,flush=True)
    assert not git("diff","--name-only","HEAD")
    store()

result={"archives":{}}
for arch,digest in pinned.items():
    examine(arch,digest)
verify()
assert len(result["archives"])==2
