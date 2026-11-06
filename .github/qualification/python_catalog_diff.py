import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

old = "bbbed5b2b84fe21597c740789d841e618f912def"
new = "bb983b988802b854bd03dd7a0a5220e7e7b4440e"
work = Path(os.environ["RUNNER_TEMP"]) / "python-license"
report = {"sources":{}}
out = work / "results.json"

for rev in (old, new):
    subprocess.run(["git","checkout","--quiet","--detach",rev],check=True)
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==rev
    record=report["sources"][rev]={}
    raw=subprocess.run(["timeout","-k","15","900","make","DOCKER_BUILD=false","license-check"],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    text=re.sub(r"\x1b\[[0-9;]*[A-Za-z]","",raw.stdout.decode(errors="replace"))
    record.update(exit=raw.returncode,log_sha256=hashlib.sha256(raw.stdout).hexdigest(),bytes=len(raw.stdout))
    lines=text.splitlines()
    summary=[(i,m[1]) for i,line in enumerate(lines) if (m:=re.fullmatch(r"licensecheck: (\d+) problems; regenerate tools/licensecheck/dependencies.yaml with licensecheck --mode=fetch, or amend governance/licensing.yaml",line))]
    clean=[int(m[1]) for l in lines if (m:=re.fullmatch(r"all (\d+) dependencies have up-to-date license entries that conform to governance/licensing.yaml",l))]
    if len(summary)==1:
        index,n=summary[0];n=int(n)
        assert 0<n<70 and n<=index
        problems=lines[index-n:index]
        assert all(len(p)<1500 and p.isprintable() for p in problems)
        record["problems"]=problems
    elif raw.returncode==0 and len(clean)==1:
        record.update(verified_count=clean[0],problems=[])
    else:
        record["command_errors"]=[v[:600] for v in lines if v.startswith(("licensecheck:","ERROR:"))][-6:]
    for f in ("governance/licensing.yaml","tools/licensecheck/dependencies.yaml","tools/licensecheck/licensecheck.go"):
        record[f]=hashlib.sha256(Path(f).read_bytes()).hexdigest()
    print(rev,record,flush=True)
    out.write_text(json.dumps(report,indent=2)+"\n")
assert all("problems" in r for r in report["sources"].values())
p,q=(report["sources"][s]["problems"] for s in (old,new))
report["control_only"]=sorted(set(p)-set(q))
report["candidate_only"]=sorted(set(q)-set(p))
report["shared"]=sorted(set(p)&set(q))
out.write_text(json.dumps(report,indent=2)+"\n")
