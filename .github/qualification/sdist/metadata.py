"""Capture release metadata from the two failing source distributions."""
from pathlib import Path
import gzip
import hashlib
import json
import os
import shutil
import tarfile
from sys import argv

lane=argv[1]
out=Path(os.environ["RUNNER_TEMP"])/"publication"
dist=Path("bazel-bin/sandboxexec/sandbox/python/dist")
files=sorted(dist.glob("*.tar.gz"))
if len(files)!=1:
    raise ValueError(files)
file=files[0]
buf=file.read_bytes()
tar=gzip.decompress(buf)
with tarfile.open(file) as archive:
    members=[]
    for m in archive:
        data=archive.extractfile(m).read() if m.isfile() else b""
        members.append(dict(name=m.name,mode=oct(m.mode),mtime=m.mtime,uid=m.uid,gid=m.gid,
            uname=m.uname,gname=m.gname,pax=m.pax_headers,type=m.type.decode("ascii"),linkname=m.linkname,
            size=m.size,sha256=hashlib.sha256(data).hexdigest()))
summary=dict(source=os.environ["CURRENT_PRODUCT"],compressed_sha256=hashlib.sha256(buf).hexdigest(),
    decompressed_sha256=hashlib.sha256(tar).hexdigest(),gzip_header=buf[:32].hex(),size=len(buf),members=members)
(out/f"{lane}-metadata.json").write_text(json.dumps(summary,indent=2)+"\n")
shutil.copyfile(file,out/f"{lane}.tar.gz")
