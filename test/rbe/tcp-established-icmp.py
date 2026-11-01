"""Collect only the two syscall observations and TCP continuity per system."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def observe(d):
    if not (d / "exit").exists():
        return {"verified": False}
    text=(d/"bazel.log").read_text(errors="replace")
    middle=r"initial=(\d+) poll=(\d+) so=(\d+) after=(\d+) recv=(-?\d+) errno=(\d+) post=(\d+)"
    rows={m[0]:dict(zip(("initial","poll","so_error","post_so_error","recv","errno","post_recv"), map(int,m[1:]))) for m in re.findall("ESTABLISHED_ICMP opt=([01]) "+middle, text)}
    live=sorted(set(re.findall("ESTABLISHED_LIVE opt=([01])",text)))
    be=str((d/"bep").read_text()) if (d/"bep").exists() else ""
    events=[json.loads(line) for line in be.splitlines() if line.startswith('{"')]
    inv=[x["started"]["uuid"] for x in events if "started" in x]
    summaries={x["id"]["testSummary"]["label"]:x["testSummary"]["overallStatus"] for x in events if "testSummary" in x}
    rc=int((d/"exit").read_text())
    ok=rc==0 and len(inv)==1 and len(summaries)==1 and list(summaries.values())==["PASSED"] and set(rows)=={"0","1"} and live==["0","1"] and all(r["initial"]==0 and r["so_error"]==113 and r["post_recv"]==0 for r in rows.values())
    return {"verified":ok,"exit":rc,"invocations":inv,"rows":rows,"continued":live,"summaries":summaries,"fixture_sha256":(d/"fixture").read_text().strip().split()[0],"start":(d/"start").read_text().strip(),"finish":(d/"finish").read_text().strip(),"diagnostics":[l[:300] for l in text.splitlines() if l.startswith(("ERROR:","FAIL:","[ RUN")) and not any(w in l.lower() for w in ("header","token","key","credential"))][:4]}


if __name__=="__main__":
    root=Path(sys.argv[1])
    rows={arm:observe(root/arm) for arm in ["native","netstack"]}
    ok=all(r["verified"] for r in rows.values()) and len({r.get("fixture_sha256") for r in rows.values()})==1
    (root/"verdict.json").write_text(json.dumps({"verified":ok,"source":os.environ["STOCK"],"arms":rows},indent=2)+"\n")
    if len(sys.argv)==2:
        raise SystemExit(not ok)
