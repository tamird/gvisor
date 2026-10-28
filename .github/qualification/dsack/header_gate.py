"""Verify the exact compiler headers rejected in the original build before Run."""
from hashlib import sha256
from pathlib import Path
import json
import os
import re
import subprocess

IMAGE = "gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
CONFIG = "sha256:7614139a81131983aa05e809ba1ca910c291cdb15021977e033800834fb76ce0"
ROOT = "/usr/lib/gcc/x86_64-linux-gnu/11/include"
HEADERS = ("emmintrin.h","limits.h","mm_malloc.h","mmintrin.h","mwaitintrin.h","pmmintrin.h","stdarg.h","stdbool.h","stddef.h","stdint.h","syslimits.h","tmmintrin.h","xmmintrin.h")
ORIGINAL = "0032577c3bdd0c02c0f46e622fa954c685789741fd385857004f752999850dee"


def main():
    out=Path(os.environ["RUNNER_TEMP"])/"dsack-comparison"
    row={"status":"FAIL","image":IMAGE,"original_failure_detail_sha256":ORIGINAL}
    try:
        cmd = 'set -eu; uname -m; /usr/bin/gcc -dumpfullversion; inc=$(/usr/bin/gcc -print-file-name=include); readlink -f "$inc"; for h do test -s "$inc/$h"; sha256sum "$inc/$h"; done'
        child=subprocess.run(["docker","run","--rm","--network=none","--read-only","--pull=always","--platform=linux/amd64","--entrypoint","/bin/sh",IMAGE,"-c",cmd,"sh",*HEADERS],stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,timeout=480)
        row["exit"]=child.returncode
        if child.returncode:row["docker_error_sha256"]=sha256(child.stderr).hexdigest();raise ValueError("worker image verification failed")
        arch,version,inc,*rows=child.stdout.decode().splitlines()
        assert arch=="x86_64" and re.fullmatch(r"11[.]\d+[.]\d+",version) and inc==ROOT and len(rows)==len(HEADERS)
        hashes={}
        for h,s in zip(HEADERS,rows):
            m=re.fullmatch(r"([0-9a-f]{64})  (.+)",s);assert m and m[2]==ROOT+"/"+h
            hashes[h]=m[1]
        cfg=subprocess.check_output(["docker","image","inspect","--format","{{.Id}}",IMAGE],text=True).strip()
        assert cfg==CONFIG
        row.update(status="PASS",config=cfg,arch=arch,gcc=version,include=inc,headers=hashes)
    finally:
        b=(json.dumps(row,indent=2)+"\n").encode()
        (out/"gcc-gate.json").write_bytes(b)
        if row["status"]!="PASS":(out/"verdict.json").write_bytes(b)


if __name__=="__main__":main()
